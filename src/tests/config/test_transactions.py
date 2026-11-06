from __future__ import annotations

import fcntl
import json
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from config import Config
from config.transaction import ConfigTransaction
from errors import SchemaValidationError

if TYPE_CHECKING:
    from config import AppConfig, JsonDict, JsonValue


def test_transaction_commit_writes_nested_changes(cfg: Config) -> None:
    with cfg as tx:
        tx["panel"] = {"name": "local"}
        panel = tx["panel"]
        assert isinstance(panel, dict)
        panel["port"] = 2053
    assert cfg["panel"] == {"name": "local", "port": 2053}


@pytest.mark.parametrize(("before", "after"), [(1, True), (True, 1), (1, 1.0), (1.0, 1)])
def test_transaction_commits_nested_type_only_changes(
    cfg: Config, before: float | bool, after: float | bool
) -> None:
    cfg["nested"] = {"values": [{"value": before}]}
    with cfg.edit() as tx:
        nested = tx["nested"]
        assert isinstance(nested, dict)
        values = nested["values"]
        assert isinstance(values, list)
        item = values[0]
        assert isinstance(item, dict)
        item["value"] = after

    live = cast(dict[str, list[dict[str, float | bool]]], cfg.view()["nested"])
    disk = json.loads(Path(cfg.path).read_text(encoding="utf-8"))["nested"]
    for document in (live, disk):
        value = document["values"][0]["value"]
        assert type(value) is type(after)
        assert value == after


def test_transaction_validates_nested_type_only_changes(app_cfg: Config[AppConfig]) -> None:
    before = Path(app_cfg.path).read_bytes()
    with pytest.raises(SchemaValidationError), app_cfg.edit() as tx:
        tx.view()["3xui"]["local"]["inbounds_list"][0] = True
    assert Path(app_cfg.path).read_bytes() == before
    value = app_cfg.view()["3xui"]["local"]["inbounds_list"][0]
    assert type(value) is int
    assert value == 1


def test_transaction_exception_does_not_persist(cfg: Config) -> None:
    cfg["keep"] = 1

    def abort_transaction() -> None:
        with cfg as tx:
            tx["keep"] = 2
            raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        abort_transaction()
    assert cfg["keep"] == 1


def test_isolate_commits_detaches_leaked_transaction_refs(tmp_path: Path) -> None:
    cfg = Config(
        path=tmp_path / "config.json",
        backup_dir=None,
        sync_mode="none",
        isolate_commits=True,
    )
    try:
        with cfg as tx:
            tx["nested"] = {"a": 1}
            leaked = tx["nested"]
            assert isinstance(leaked, dict)
        leaked["a"] = 2
        assert cfg["nested"] == {"a": 1}
    finally:
        cfg.close()


def test_disabled_isolate_commits_keeps_leaked_transaction_refs(tmp_path: Path) -> None:
    cfg = Config(
        path=tmp_path / "config.json",
        backup_dir=None,
        sync_mode="none",
        isolate_commits=False,
    )
    try:
        with cfg as tx:
            tx["nested"] = {"a": 1}
            leaked = tx["nested"]
            assert isinstance(leaked, dict)
        leaked["a"] = 2
        assert cfg["nested"] == {"a": 2}
    finally:
        cfg.close()


def test_direct_access_inside_transaction_is_rejected(cfg: Config) -> None:
    with cfg as tx:
        tx["x"] = 1
        with pytest.raises(RuntimeError, match="transaction"):
            _ = cfg["x"]


def test_nested_transactions_are_rejected(cfg: Config) -> None:
    with cfg.edit() as tx:
        tx["x"] = 1
        with pytest.raises(RuntimeError, match="Nested"):
            with cfg.edit() as inner:
                inner["x"] = 2


@pytest.mark.parametrize("outer_style", ["edit", "context"])
@pytest.mark.parametrize("inner_style", ["edit", "context", "same"])
@pytest.mark.parametrize("rollback", [False, True])
def test_nested_rejection_preserves_outer_transaction(
    cfg: Config, outer_style: str, inner_style: str, rollback: bool
) -> None:
    cfg["x"] = 0
    outer = cfg.edit() if outer_style == "edit" else cfg

    with pytest.raises(ValueError, match="abort") if rollback else nullcontext():
        with outer as tx:
            tx["x"] = 1
            inner = cfg.edit() if inner_style == "edit" else cfg if inner_style == "context" else tx
            with pytest.raises(RuntimeError, match="Nested"):
                with inner:
                    pytest.fail("nested transaction unexpectedly entered")

            # Check ownership first, so a broken guard cannot deadlock on the file lock.
            assert cfg.active_transaction is tx
            assert tx.owner_thread_id == threading.get_ident()
            if outer_style == "context":
                assert cfg.context_transaction is tx
            with pytest.raises(RuntimeError, match="transaction"):
                _ = cfg["x"]
            with pytest.raises(RuntimeError, match="Nested"):
                cfg["x"] = 3
            tx["x"] = 2
            if rollback:
                raise ValueError("abort")

    acquired = threading.Event()

    def check_thread_lock() -> None:
        if cfg.lock.acquire(timeout=1):
            acquired.set()
            cfg.lock.release()

    thread = threading.Thread(target=check_thread_lock, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert acquired.is_set(), "config lock was not released"
    with Path(cfg.lockfile_path).open("a+b") as lock_fp:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fp, fcntl.LOCK_UN)

    expected = 0 if rollback else 2
    assert cfg["x"] == expected
    assert json.loads(Path(cfg.path).read_text(encoding="utf-8"))["x"] == expected
    with cfg.edit() as tx:
        tx["x"] = 3
    assert cfg["x"] == 3


def test_transaction_view_is_active_and_commits_nested_edits(app_cfg: Config[AppConfig]) -> None:
    transaction = ConfigTransaction(app_cfg)
    with pytest.raises(RuntimeError, match="not active"):
        transaction.view()
    with transaction as tx:
        snapshot: AppConfig = tx.view()
        assert type(snapshot) is dict
        snapshot["3xui"]["local"]["inbounds_list"].append(2)
        assert tx.view() is snapshot
        copied: JsonDict = tx.copy()
        assert copied == snapshot
        assert copied is not snapshot
        with pytest.raises(RuntimeError, match="transaction"):
            app_cfg.view()
    with pytest.raises(RuntimeError, match="not active"):
        transaction.view()
    assert app_cfg.view()["3xui"]["local"]["inbounds_list"] == [1, 2]
    with app_cfg as context_tx:
        context_document: AppConfig = context_tx.view()
        assert context_document["domain"] == "example.test"
    with app_cfg.edit() as edit_tx:
        edit_document: AppConfig = edit_tx.view()
        assert edit_document["domain"] == "example.test"


def test_transaction_view_rolls_back_nested_edits(app_cfg: Config[AppConfig]) -> None:
    transaction = app_cfg.edit()

    def abort_transaction() -> None:
        with transaction as tx:
            tx.view()["3xui"]["local"]["inbounds_list"].append(2)
            raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        abort_transaction()
    assert app_cfg.view()["3xui"]["local"]["inbounds_list"] == [1]
    with pytest.raises(RuntimeError, match="not active"):
        transaction.view()


@pytest.mark.parametrize("isolate", [True, False])
def test_leaked_transaction_view_obeys_commit_isolation(tmp_path: Path, isolate: bool) -> None:
    config = Config(path=tmp_path / "config.json", sync_mode="none", isolate_commits=isolate)
    try:
        with config.edit() as tx:
            leaked: JsonDict = tx.view()
            nested: dict[str, JsonValue] = {"value": 1}
            leaked["nested"] = nested
        nested["value"] = 2
        leaked["extra"] = True
        assert config.view() == (
            {"nested": {"value": 1}}
            if isolate
            else {
                "nested": {"value": 2},
                "extra": True,
            }
        )
        assert json.loads(Path(config.path).read_text(encoding="utf-8")) == {
            "nested": {"value": 1},
        }
    finally:
        config.close()

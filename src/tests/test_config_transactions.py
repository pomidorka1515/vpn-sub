from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from config import Config
from config.transaction import ConfigTransaction

if TYPE_CHECKING:
    from config import AppConfig, JsonDict, JsonValue


def test_transaction_commit_writes_nested_changes(cfg: Config) -> None:
    with cfg as tx:
        tx["panel"] = {"name": "local"}
        panel = tx["panel"]
        assert isinstance(panel, dict)
        panel["port"] = 2053
    assert cfg["panel"] == {"name": "local", "port": 2053}


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

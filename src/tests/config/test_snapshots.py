from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import pytest

from app_runtime.offload import BoundedOffload
from config import Config, JsonDict, JsonValue, LinesConfig
from config.snapshot import freeze, thaw
from config.store import AsyncConfig, AsyncLinesConfig
from errors import FileCorruptionError

if TYPE_CHECKING:
    from pathlib import Path


def _assert_immutable(snapshot: Mapping[str, JsonValue]) -> None:
    with pytest.raises(TypeError):
        cast(JsonDict, snapshot)["new"] = True
    nested = cast(JsonDict, snapshot["nested"])
    with pytest.raises(TypeError):
        nested["value"] = 2
    items = cast(list[JsonValue], nested["items"])
    with pytest.raises(TypeError):
        items[0] = None
    with pytest.raises(AttributeError):
        items.append(2)
    with pytest.raises(TypeError):
        cast(JsonDict, items[0])["inner"] = False


def _document() -> JsonDict:
    return {"nested": {"value": 1, "items": [{"inner": True}, None, "text", 1.5]}}


def test_freeze_and_thaw_detach_all_containers() -> None:
    document = _document()
    frozen = cast(Mapping[str, JsonValue], freeze(document))
    _assert_immutable(frozen)
    mutable = thaw(frozen)
    assert mutable == document
    assert isinstance(mutable, dict)
    assert isinstance(cast(JsonDict, mutable["nested"])["items"], list)
    cast(JsonDict, document["nested"])["value"] = 3
    cast(JsonDict, mutable["nested"])["value"] = 4
    assert cast(Mapping[str, JsonValue], frozen["nested"])["value"] == 1


def test_config_snapshot_reuses_and_replaces_only_changed_versions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_document()))
    config = Config(path=path, start_backup=False)
    try:
        snapshot = config.snapshot()
        _assert_immutable(snapshot)
        assert config.snapshot() is snapshot
        assert not config.reload()
        assert config.snapshot() is snapshot
        with config.edit():
            with pytest.raises(RuntimeError, match="transaction object"):
                config.snapshot()
        assert config.snapshot() is snapshot

        config["committed"] = True
        committed = config.snapshot()
        assert committed is not snapshot
        assert committed["committed"] is True
        assert "committed" not in snapshot
        detached = config.view()
        detached["committed"] = False
        assert config.snapshot() is committed
        assert committed["committed"] is True

        path.write_text('{"external": true}')
        external = config.snapshot()
        assert external == {"external": True}
        assert external is config.snapshot()
        assert committed["committed"] is True
        path.write_text("broken")
        with pytest.raises(FileCorruptionError):
            config.snapshot()
        assert config._shared_snapshot is external
    finally:
        config.close()


@pytest.mark.asyncio
async def test_async_snapshot_publication_and_zero_copy_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_document()))
    files = BoundedOffload(capacity=2, name="test-files")
    store = AsyncConfig(lambda: Config(path=path, start_backup=False), files=files)
    with pytest.raises(RuntimeError, match="not started"):
        store.snapshot()
    await store.start()
    try:
        original = store.snapshot()
        _assert_immutable(original)
        assert not await store.reload()
        assert store.snapshot() is original
        await store.mutate(lambda tx: None)
        assert store.snapshot() is original

        def fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("snapshot reads must not copy or access files") from None

        with monkeypatch.context() as patches:
            patches.setattr("config.snapshot.freeze", fail)
            patches.setattr("config.snapshot._thaw", fail)
            patches.setattr("config.store.copy.deepcopy", fail)
            patches.setattr(files, "run", fail)
            for _ in range(10):
                assert store.snapshot() is original

        path.write_text("broken")
        assert store.snapshot() is original
        with pytest.raises(FileCorruptionError):
            await store.reload()
        assert store.snapshot() is original
        path.write_text('{"external": true}')
        assert await store.reload()
        external = store.snapshot()
        assert external == {"external": True}
        assert "external" not in original
        await store.mutate(lambda tx: tx.__setitem__("committed", True))
        committed = store.snapshot()
        assert committed == {"external": True, "committed": True}
        assert external == {"external": True}
        mutable = store.view()
        mutable["committed"] = False
        assert store.snapshot() is committed
        assert committed["committed"] is True

        with pytest.raises(ValueError, match="rejected"):
            await store.mutate(lambda tx: _reject())
        assert store.snapshot() is committed
    finally:
        await store.close()
        await files.close()


def _reject() -> None:
    raise ValueError("rejected") from None


def test_lines_snapshot_tracks_writes_external_changes_and_compaction(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    with LinesConfig(path, start_backup=False, sync_mode="none") as store:
        assert store.snapshot() == ()
        store.append(_document())
        original = store.snapshot()
        _assert_immutable(original[0])
        assert store.snapshot() is original
        mutable = store.read_all()
        mutable[0]["nested"] = None
        assert store.snapshot() is original
        store.append({"extra": True})
        appended = store.snapshot()
        assert appended is not original
        assert len(appended) == 2
        assert len(original) == 1
        store.compact(lambda record: "extra" in record)
        assert store.snapshot() == ({"extra": True},)
        store.clear()
        assert store.snapshot() == ()
        path.write_text('{"external": true}\n')
        external = store.snapshot()
        assert external == ({"external": True},)
        assert store.snapshot() is external
        path.write_text("broken\n")
        with pytest.raises(json.JSONDecodeError):
            store.snapshot()
        assert store._snapshot is external


def test_lines_snapshot_tracks_size_limit_purge(tmp_path: Path) -> None:
    with LinesConfig(
        tmp_path / "records.jsonl", start_backup=False, sync_mode="none", max_size_mb=1
    ) as store:
        store.append({"old": True})
        original = store.snapshot()
        store.append({"large": "x" * 1_000_000})
        assert store.snapshot() == ()
        assert original == ({"old": True},)
        store.append_many([{"new": True}, {"last": True}])
        assert store.snapshot() == ({"new": True}, {"last": True})


@pytest.mark.asyncio
async def test_async_lines_snapshot_is_shared_and_immutable(tmp_path: Path) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    store = AsyncLinesConfig(
        lambda: LinesConfig(tmp_path / "records.jsonl", start_backup=False), files=files
    )
    await store.start()
    try:
        await store.append(_document())
        original = await store.snapshot()
        _assert_immutable(original[0])
        assert await store.snapshot() is original
        await store.append({"extra": True})
        assert len(await store.snapshot()) == 2
        assert len(original) == 1
    finally:
        await store.close()
        await files.close()

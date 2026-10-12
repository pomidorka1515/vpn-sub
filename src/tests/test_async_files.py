from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING

import pytest

from app_runtime.offload import BoundedOffload
from config import Config, LinesConfig
from config.async_store import AsyncConfig, AsyncLinesConfig, FileSchedules
from core.services.async_audit import AsyncAuditService
from errors import FileCorruptionError
from loggers.async_delivery import AsyncJSONLinesLogger, AsyncTelegramLogger, LogDelivery

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from pathlib import Path

    from config import JsonDict, JsonValue

pytestmark = pytest.mark.asyncio


async def test_snapshots_reload_failure_and_atomic_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"nested": {"value": 1}}')
    files = BoundedOffload(capacity=2, name="test-files")
    source = Config(path=path, start_backup=False)
    store = AsyncConfig(lambda: source, files=files, reload_interval=3600)
    await store.start()
    try:
        snapshot = store.view()
        snapshot["nested"] = None
        # Reads cannot consult the underlying file at all.
        path.write_text("broken")
        assert store.view() == {"nested": {"value": 1}}
        with pytest.raises(FileCorruptionError):
            await store.reload()
        assert store.view() == {"nested": {"value": 1}}
        path.write_text('{"nested": {"value": 2}, "external": true}')
        await store.mutate(lambda tx: tx.__setitem__("new", 3))
        assert store.view() == {"nested": {"value": 2}, "external": True, "new": 3}

        def invalid(tx: object) -> None:
            raise ValueError("invalid edit")

        with pytest.raises(ValueError, match="invalid edit"):
            await store.mutate(invalid)
        assert store.view()["new"] == 3

        def write_failure(data: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(source, "atomic_write", write_failure)
        with pytest.raises(OSError, match="disk full"):
            await store.mutate(lambda tx: tx.__setitem__("new", 4))
        assert store.view()["new"] == 3
        assert json.loads(path.read_text())["new"] == 3
    finally:
        await store.close()
        await store.close()
        await files.close()


async def test_cancelled_commit_finishes_and_close_drains(tmp_path: Path) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    store = AsyncConfig(
        lambda: Config(path=tmp_path / "config.json", start_backup=False), files=files,
    )
    await store.start()
    entered = threading.Event()
    release = threading.Event()

    def edit(tx: MutableMapping[str, JsonValue]) -> None:
        entered.set()
        assert release.wait(5)
        tx["committed"] = True

    task = asyncio.create_task(store.mutate(edit))
    await asyncio.to_thread(entered.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert store.view() == {"committed": True}
    await files.close()


async def test_audit_acknowledges_fsync_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    path = tmp_path / "audit.jsonl"
    store = AsyncLinesConfig(lambda: LinesConfig(path, start_backup=False), files=files)
    await store.start()
    audit = AsyncAuditService(store)
    synced = threading.Event()

    def fsync(fd: int) -> None:
        synced.set()

    monkeypatch.setattr("config.lines.os.fsync", fsync)
    await audit.audit(name="user_add", info={"username": "alice"})
    assert synced.is_set()
    assert json.loads(path.read_text())["info"] == {"username": "alice"}

    def fail(fd: int) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr("config.lines.os.fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        await audit.audit(name="user_delete")
    await store.close()
    await files.close()


async def test_thread_logging_bounded_delivery_and_drain() -> None:
    received: list[JsonDict] = []
    fallback: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(record: JsonDict) -> None:
        entered.set()
        await release.wait()
        received.append(record)

    delivery = LogDelivery(deliver, name="test-logs", capacity=1, fallback=fallback.append)
    await delivery.start()
    handler = AsyncJSONLinesLogger(delivery)

    def emit(message: str) -> None:
        handler.handle(logging.LogRecord("test", logging.ERROR, __file__, 1, message, (), None))

    await asyncio.to_thread(emit, "first")
    await entered.wait()
    await asyncio.to_thread(emit, "second")
    await asyncio.to_thread(emit, "overflow")
    assert delivery.dropped == 1
    assert fallback == ["overflow"]
    close = asyncio.create_task(delivery.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert [record["text"] for record in received] == ["first", "second"]
    assert all(record["threadname"] != "MainThread" for record in received)


async def test_log_delivery_failure_no_recursion_and_timeout() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    failures: list[str] = []

    async def deliver(value: str) -> None:
        entered.set()
        await release.wait()
        handler.handle(logging.LogRecord("test", logging.ERROR, __file__, 1, "recursive", (), None))
        raise OSError("secret-token")

    delivery = LogDelivery(deliver, name="alerts", drain_timeout=0.01, fallback=failures.append)
    handler = AsyncTelegramLogger(delivery)
    await delivery.start()
    delivery.submit("message", important=False, fallback_text="message")
    await entered.wait()
    with pytest.raises(TimeoutError):
        await delivery.close()
    release.set()
    await delivery.close()
    assert delivery.failed == 1
    assert failures == ["alerts: log delivery failed"]


async def test_owned_schedules_and_backup(tmp_path: Path) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    store = AsyncConfig(
        lambda: Config(
            path=tmp_path / "config.json",
            start_backup=False,
            backup_dir=tmp_path / "backups",
            backup_interval=0.01,
        ),
        files=files,
        reload_interval=0.01,
    )
    await store.start()
    await store.backup_now()
    assert list((tmp_path / "backups").rglob("*.json"))
    await store.close()
    assert store._schedules.tasks.active_count == 0
    await files.close()

    schedules = FileSchedules()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    schedules.add(job, interval=0.001, name="test:job")
    await entered.wait()
    await asyncio.sleep(0.01)
    assert calls == 1
    await schedules.close()

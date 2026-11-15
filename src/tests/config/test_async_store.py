from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING

import pytest

from app_runtime.offload import BoundedOffload
from config import Config, LinesConfig
from config.store import AsyncConfig, AsyncLinesConfig, FileSchedules
from core.services.audit import AuditService
from errors import FileCorruptionError, SchemaValidationError
from loggers.delivery import AsyncTelegramLogger, LogDelivery
from threadutil import named_to_thread

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from pathlib import Path

    from config import JsonValue

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_reload_and_commit_preserve_snapshot(tmp_path: Path, number: float) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"valid": 1}')
    files = BoundedOffload(capacity=2, name="test-files")
    store = AsyncConfig(lambda: Config(path=path, start_backup=False), files=files)
    await store.start()
    try:
        original = path.read_bytes()
        with pytest.raises(SchemaValidationError, match="non-finite"):
            await store.mutate(lambda tx: tx.__setitem__("extension", [{"value": number}]))
        assert store.view() == {"valid": 1}
        assert path.read_bytes() == original

        invalid = json.dumps({"extension": number})
        path.write_text(invalid)
        with pytest.raises(FileCorruptionError):
            await store.reload()
        assert store.view() == {"valid": 1}
        assert path.read_text() == invalid
    finally:
        await store.close()
        await files.close()


async def test_snapshots_reload_failure_and_atomic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        lambda: Config(path=tmp_path / "config.json", start_backup=False),
        files=files,
    )
    await store.start()
    entered = threading.Event()
    release = threading.Event()

    def edit(tx: MutableMapping[str, JsonValue]) -> None:
        entered.set()
        assert release.wait(5)
        tx["committed"] = True

    task = asyncio.create_task(store.mutate(edit))
    await named_to_thread("test:files:wait", entered.wait)
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    path = tmp_path / "audit.jsonl"
    store = AsyncLinesConfig(lambda: LinesConfig(path, start_backup=False), files=files)
    await store.start()
    audit = AuditService(store)
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


async def test_hit_audit_burst_batches_and_drains_without_waiting_for_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    path = tmp_path / "audit.jsonl"
    store = AsyncLinesConfig(lambda: LinesConfig(path, start_backup=False), files=files)
    await store.start()
    audit = AuditService(store)
    entered = threading.Event()
    release = threading.Event()
    syncs = 0

    def fsync(fd: int) -> None:
        nonlocal syncs
        syncs += 1
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr("config.lines.os.fsync", fsync)
    try:
        for index in range(320):
            audit.audit_hit(info={"username": str(index)})
        await named_to_thread("test:audit:wait", entered.wait)
        assert store._operations.active_count == 0
        assert store.dropped == 0
        close = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        assert not close.done()
        # Admission has closed, without failing the subscription caller.
        audit.audit_hit(info={"username": "late"})
        assert store.dropped == 1
        release.set()
        await close
        assert syncs == 2
        assert len(path.read_text().splitlines()) == 320
        assert store.failed == 0
    finally:
        release.set()
        await store.close()
        await files.close()


async def test_buffer_overflow_detaches_records_and_disk_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = BoundedOffload(capacity=2, name="test-files")
    path = tmp_path / "audit.jsonl"
    store = AsyncLinesConfig(
        lambda: LinesConfig(path, start_backup=False), files=files, buffer_capacity=2
    )
    await store.start()
    record: dict[str, JsonValue] = {"value": [1]}
    assert store.enqueue(record)
    record["value"] = [2]
    assert store.enqueue(record)
    assert not store.enqueue(record)
    await store.close()
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"value": [1]},
        {"value": [2]},
    ]
    assert store.dropped == 1

    def fail(fd: int) -> None:
        raise OSError("disk failure")

    store = AsyncLinesConfig(lambda: LinesConfig(path, start_backup=False), files=files)
    await store.start()
    monkeypatch.setattr("config.lines.os.fsync", fail)
    assert store.enqueue(record)
    await store.close()
    assert store.failed == 1
    await files.close()


async def test_thread_logging_bounded_delivery_and_drain() -> None:
    received: list[str] = []
    fallback: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(record: str) -> None:
        entered.set()
        await release.wait()
        received.append(record)

    delivery = LogDelivery(deliver, name="test-logs", capacity=1, fallback=fallback.append)
    await delivery.start()
    handler = AsyncTelegramLogger(delivery)

    def emit(message: str) -> None:
        handler.handle(logging.LogRecord("test", logging.ERROR, __file__, 1, message, (), None))

    await named_to_thread("test:logs:emit", emit, "first")
    await entered.wait()
    await named_to_thread("test:logs:emit", emit, "second")
    await named_to_thread("test:logs:emit", emit, "overflow")
    assert delivery.dropped == 1
    assert fallback == ["overflow"]
    close = asyncio.create_task(delivery.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert received == ["<code>first</code>", "<code>second</code>"]


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

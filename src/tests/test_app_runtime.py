from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest

from app_runtime import (
    AdmissionClosedError,
    BoundedOffload,
    CapacityExceededError,
    MutationRegistry,
    OwnedResource,
    Runtime,
    RuntimeState,
    TaskRegistry,
)


@dataclass
class FakeResource:
    name: str
    events: list[str]
    fail_start: bool = False
    fail_close: bool = False

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise ValueError("startup failed")

    async def close(self) -> None:
        self.events.append(f"close:{self.name}")
        if self.fail_close:
            raise ValueError("cleanup failed")


@pytest.mark.asyncio
async def test_start_failure_unwinds_partial_resource_in_reverse_order() -> None:
    events: list[str] = []
    resources = tuple(OwnedResource(name, FakeResource(name, events, fail_start=name == "panels"))
                      for name in ("lock", "database", "panels", "bots"))
    runtime = Runtime(object(), resources=resources)
    assert events == []
    with pytest.raises(ValueError, match="startup failed"):
        await runtime.start()
    await runtime.stop()
    assert events == ["start:lock", "start:database", "start:panels",
                      "close:panels", "close:database", "close:lock"]
    assert runtime.state is RuntimeState.STOPPED
    assert not runtime.ready


@pytest.mark.asyncio
async def test_repeated_stop_and_cleanup_failure_visibility() -> None:
    events: list[str] = []
    runtime = Runtime("dependencies", resources=(
        OwnedResource("lock", FakeResource("lock", events)),
        OwnedResource("database", FakeResource("database", events, fail_close=True)),
    ))
    await runtime.start()
    await runtime.start()
    assert runtime.require_ready() == "dependencies"
    for _ in range(2):
        with pytest.raises(ExceptionGroup, match="runtime cleanup failed"):
            await runtime.stop()
    assert events == ["start:lock", "start:database", "close:database", "close:lock"]
    with pytest.raises(AdmissionClosedError):
        runtime.require_ready()
    with pytest.raises(AdmissionClosedError):
        await runtime.start()


@pytest.mark.asyncio
async def test_cancelled_mutation_caller_detaches_and_stop_waits_before_close() -> None:
    events: list[str] = []
    started = asyncio.Event()
    finish = asyncio.Event()
    runtime = Runtime(object(), resources=(OwnedResource("db", FakeResource("db", events)),))
    await runtime.start()

    async def mutation() -> int:
        started.set()
        await finish.wait()
        events.append("mutation:done")
        return 42

    caller = asyncio.create_task(runtime.mutations.run(mutation, name="delete:user"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert runtime.mutations.active_count == 1
    stopping = asyncio.create_task(runtime.stop())
    await runtime.stopping.wait()
    assert not runtime.ready
    with pytest.raises(AdmissionClosedError):
        await runtime.mutations.run(mutation, name="late")
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert events == ["start:db"]
    finish.set()
    await runtime.stop()
    assert events == ["start:db", "mutation:done", "close:db"]


@pytest.mark.asyncio
async def test_mutation_admission_is_bounded_and_pre_cancel_does_not_run() -> None:
    mutations = MutationRegistry(capacity=1)
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()

    caller = asyncio.create_task(mutations.run(operation, name="first"))
    await started.wait()
    with pytest.raises(CapacityExceededError):
        await mutations.run(operation, name="excess")
    cancelled = asyncio.create_task(mutations.run(operation, name="cancelled"))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert calls == 1
    finish.set()
    await caller
    await mutations.drain()
    assert mutations.active_count == 0


@pytest.mark.asyncio
async def test_offload_retains_capacity_and_drains_cancelled_callers() -> None:
    offload = BoundedOffload(capacity=1, name="disk")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finish = threading.Event()
    completed = threading.Event()

    def blocking() -> int:
        loop.call_soon_threadsafe(started.set)
        if not finish.wait(timeout=5):
            raise TimeoutError("test did not release worker")
        completed.set()
        return 42

    caller = asyncio.create_task(offload.run(blocking))
    try:
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        with pytest.raises(CapacityExceededError):
            await offload.run(lambda: 0)
        closing = asyncio.create_task(offload.close())
        await asyncio.sleep(0)
        assert not closing.done()
        assert not completed.is_set()
        finish.set()
        await closing
        assert completed.is_set()
        assert offload.active_count == 0
        with pytest.raises(AdmissionClosedError):
            await offload.run(lambda: 0)
    finally:
        finish.set()
        await offload.close()


@pytest.mark.asyncio
async def test_owned_background_tasks_cancel_and_report_detached_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks = TaskRegistry()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def background() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def failing() -> None:
        raise ValueError("detached failure")

    tasks.create(background, name="watcher")
    await started.wait()
    failed = tasks.create(failing, name="health")
    await asyncio.gather(failed, return_exceptions=True)
    await tasks.drain(cancel=True)
    assert cleaned.is_set()
    assert tasks.active_count == 0
    assert "owned task health failed" in caplog.text


@pytest.mark.asyncio
async def test_startup_cancellation_closes_partial_resource() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class PartialResource:
        async def start(self) -> None:
            started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            closed.set()

    runtime = Runtime(object(), resources=(OwnedResource("partial", PartialResource()),))
    startup = asyncio.create_task(runtime.start())
    await started.wait()
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    await asyncio.gather(runtime.stop(), runtime.stop())
    assert closed.is_set()
    assert runtime.state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_stop_before_start_rejects_startup() -> None:
    runtime = Runtime(object())
    await asyncio.gather(runtime.stop(), runtime.stop())
    with pytest.raises(AdmissionClosedError):
        await runtime.start()


@pytest.mark.asyncio
async def test_stop_during_failed_startup_unwinds_once() -> None:
    started = asyncio.Event()
    fail = asyncio.Event()
    closed = 0

    class FailingResource:
        async def start(self) -> None:
            started.set()
            await fail.wait()
            raise ValueError("startup failed")

        async def close(self) -> None:
            nonlocal closed
            closed += 1

    runtime = Runtime(object(), resources=(OwnedResource("partial", FailingResource()),))
    startup = asyncio.create_task(runtime.start())
    await started.wait()
    stopping = asyncio.create_task(runtime.stop())
    await runtime.stopping.wait()
    fail.set()
    with pytest.raises(ValueError, match="startup failed"):
        await startup
    await stopping
    assert closed == 1
    assert runtime.state is RuntimeState.STOPPED

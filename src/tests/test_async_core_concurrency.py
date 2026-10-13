from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID, FakePanel, make_inbound, make_panel_client

from app_runtime.offload import BoundedOffload
from app_runtime.tasks import CapacityExceededError, MutationRegistry, TaskRegistry
from core.coordination import MutationCoordinator
from db.database import Database
from errors import PanelRejectedError
from session.cache import GenerationCache
from session.stamp import bump_stamp

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    from config import JsonValue
    from session.client import XUiSession

import pytest_asyncio


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database]:
    database = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await database.start()
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_registration_settles_siblings_then_compensates(
    db: Database,
    tmp_path: Path,
) -> None:
    accepted = FakePanel(name="accepted", inbounds=[make_inbound(1)])
    rejected = FakePanel(name="rejected", inbounds=[make_inbound(1)])
    sub = await make_async_subscription(db, tmp_path, [accepted, rejected])
    started, release = asyncio.Event(), asyncio.Event()
    original = sub.business_svc._post

    async def post(
        panel: XUiSession,
        route: str,
        body: Mapping[str, JsonValue],
        what: str,
    ) -> None:
        if route.endswith("clients/add") and panel is sub.panels[0]:
            await original(panel, route, body, what)
            started.set()
            await release.wait()
        elif route.endswith("clients/add"):
            await started.wait()
            raise PanelRejectedError("second panel rejected")
        else:
            await original(panel, route, body, what)

    sub.business_svc._post = post  # type: ignore[method-assign]
    task: asyncio.Task[object] | None = None
    try:
        await sub.code_svc.add_code("invite", "register")
        task = asyncio.create_task(
            sub.business_code_svc.register_with_code(
                code="invite",
                username="alice",
                displayname="Alice",
                ext_username="alice",
                ext_password="secret",
            ),
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert accepted.clients
        assert await db.user_exists("alice")
        assert sub.res.coordinator.registry.active_count == 1
        # Compensation cannot start while the accepted sibling is unsettled.
        assert not any("clients/del" in route for route, _ in accepted.posts)
        release.set()
        await sub.close()
        assert accepted.clients == []
        assert not await db.user_exists("alice")
        code = await db.get_code("invite")
        assert code is not None
        assert code["uses"] == 1
        assert sub.res.coordinator.lock_count == 0
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_reset_delete_race_does_not_resurrect_client(
    db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])], inbounds=[make_inbound(1)])
    sub = await make_async_subscription(db, tmp_path, [panel])
    await db.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
    )
    started, release = asyncio.Event(), asyncio.Event()
    original = sub.business_svc._post

    async def update(
        panel: XUiSession,
        route: str,
        body: Mapping[str, JsonValue],
        what: str,
    ) -> None:
        started.set()
        await release.wait()
        await original(panel, route, body, what)

    # Only pause UUID update; restore the method before delete acquires ownership.
    sub.business_svc._post = update  # type: ignore[method-assign]
    reset = asyncio.create_task(sub.business_svc.reset_user("alice"))
    delete: asyncio.Task[None] | None = None
    try:
        await started.wait()
        delete = asyncio.create_task(sub.business_svc.delete_user("alice", perma=True))
        await asyncio.sleep(0)
        assert panel.clients
        assert not delete.done()
        sub.business_svc._post = original  # type: ignore[method-assign]
        release.set()
        await reset
        await delete
        assert panel.clients == []
        assert not await db.user_exists("alice")
        assert sub.res.coordinator.lock_count == 0
    finally:
        release.set()
        await asyncio.gather(reset, *([delete] if delete else []), return_exceptions=True)
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_fanout_waits_for_later_write_before_raising(
    db: Database,
    tmp_path: Path,
) -> None:
    sub = await make_async_subscription(
        db,
        tmp_path,
        [FakePanel(name="first"), FakePanel(name="later")],
    )
    started, release = asyncio.Event(), asyncio.Event()
    finished: list[str] = []

    async def write(panel: object) -> None:
        if panel is sub.panels[0]:
            await started.wait()
            raise PanelRejectedError("first failed")
        started.set()
        await release.wait()
        finished.append("later")

    task = asyncio.create_task(sub.panel_svc.map_panels(sub.panels, write))
    try:
        await started.wait()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(PanelRejectedError, match="first failed"):
            await task
        assert finished == ["later"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_background_capacity_does_not_block_foreground(
    db: Database,
    tmp_path: Path,
) -> None:
    sub = await make_async_subscription(
        db,
        tmp_path,
        [FakePanel(name=str(index)) for index in range(12)],
    )
    started, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def background(panel: object) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        await release.wait()
        active -= 1
        return "background"

    async def foreground(panel: object) -> str:
        return "foreground"

    task = asyncio.create_task(sub.panel_svc.map_panels(sub.panels, background, work="background"))
    try:
        await started.wait()
        assert await sub.panel_svc.map_panels(sub.panels[:1], foreground) == ["foreground"]
        release.set()
        assert await task == ["background"] * 12
        assert peak == 4
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_cache_invalidation_and_cancelled_single_flight_waiter(tmp_path: Path) -> None:
    files = BoundedOffload(capacity=1, name="test-files")
    tasks = TaskRegistry()
    cache = GenerationCache[list[str]](
        str(tmp_path / "clients.stamp"),
        files=files,
        tasks=tasks,
    )
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load() -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return ["old"]
        return ["new"]

    first = asyncio.create_task(cache.get(60, load))
    second: asyncio.Task[list[str]] | None = None
    try:
        await started.wait()
        second = asyncio.create_task(cache.get(60, load))
        # Wait until the second caller has joined the existing refresh.
        async with cache._lock:
            pass
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == ["old"]
        assert calls == 1
        await cache.clear()
        assert await cache.get(60, load) == ["new"]
        assert calls == 2
        # Returned containers are detached.
        (await cache.get(60, load)).append("caller mutation")
        assert await cache.get(60, load) == ["new"]
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await tasks.drain()
        await files.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_refresh_cannot_publish_after_generation_changes(
    tmp_path: Path,
    external: bool,
) -> None:
    files = BoundedOffload(capacity=1, name="test-files")
    tasks = TaskRegistry()
    path = str(tmp_path / "clients.stamp")
    cache = GenerationCache[str](path, files=files, tasks=tasks)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return "old"
        return "new"

    first = asyncio.create_task(cache.get(60, load))
    try:
        await started.wait()
        if external:
            await files.run(bump_stamp, path)
        else:
            await cache.clear()
        assert await cache.get(60, load) == "new"
        release.set()
        assert await first == "old"
        assert await cache.get(60, load) == "new"
        assert calls == 2
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await tasks.drain()
        await files.close()


@pytest.mark.asyncio
async def test_ordered_multi_user_locks_bounded_admission_and_forced_cancel() -> None:
    registry = MutationRegistry(capacity=2)
    coordinator = MutationCoordinator(registry)
    started, release = asyncio.Event(), asyncio.Event()
    finished: list[str] = []

    async def first() -> None:
        started.set()
        await release.wait()
        finished.append("first")

    async def second() -> None:
        finished.append("second")

    one = asyncio.create_task(coordinator.run(["bob", "alice"], first, name="first"))
    two: asyncio.Task[None] | None = None
    drain: asyncio.Task[None] | None = None
    try:
        await started.wait()
        two = asyncio.create_task(coordinator.run(["alice", "bob"], second, name="second"))
        # Ensure the second registry task is admitted before saturation check.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(CapacityExceededError):
            await coordinator.run(["other"], second, name="excess")
        drain = asyncio.create_task(registry.drain(cancel=True))
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        await drain
        await asyncio.gather(one, two, return_exceptions=True)
        assert finished == ["first", "second"]
        assert coordinator.lock_count == 0
    finally:
        release.set()
        await asyncio.gather(
            one,
            *([two] if two else []),
            *([drain] if drain else []),
            return_exceptions=True,
        )
        await registry.drain()


@pytest.mark.asyncio
async def test_quota_rechecks_limit_after_waiting_for_admin_mutation(
    db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_async_subscription(db, tmp_path, [panel])
    await db.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
    )
    await db.increment_usage("alice", regular=2 * 10**9)
    started, release = asyncio.Event(), asyncio.Event()

    async def admin() -> None:
        started.set()
        await release.wait()
        await sub.business_svc.update_params("alice", limit=10)

    edit = asyncio.create_task(sub.res.coordinator.run(["alice"], admin, name="admin"))
    quota: asyncio.Task[None] | None = None
    try:
        await started.wait()
        quota = asyncio.create_task(sub.business_svc.apply_quota("alice"))
        await asyncio.sleep(0)
        release.set()
        await edit
        await quota
        user = await db.get_user("alice")
        assert user is not None
        assert user["bw_limit_gb"] == 10
        assert user["enabled"]
        assert panel.clients[0].enable
        assert not any("bulkDisable" in route for route, _ in panel.posts)
    finally:
        release.set()
        await asyncio.gather(edit, *([quota] if quota else []), return_exceptions=True)
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_failed_shared_refresh_is_retryable(tmp_path: Path) -> None:
    files = BoundedOffload(capacity=1, name="cache-files")
    tasks = TaskRegistry()
    cache = GenerationCache[str](str(tmp_path / "stamp"), files=files, tasks=tasks)
    calls = 0

    async def load() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("refresh failed")
        return "recovered"

    try:
        with pytest.raises(RuntimeError, match="refresh failed"):
            await cache.get(60, load)
        assert await cache.get(60, load) == "recovered"
        assert calls == 2
    finally:
        await tasks.drain()
        await files.close()

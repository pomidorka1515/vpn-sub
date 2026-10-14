from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from async_api_helpers import OpenRedis
from quart import request

from api.application import create_api_application
from app import AppOptions, AppPaths, create_application, verbose_overrides
from app_runtime.lifecycle import RuntimeState
from app_runtime.rate_limit import AsyncRateLimiter
from discord_bot.host import SharedDiscordClient


def test_verbose_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in ("bwatch", "bwatch,", ",bwatch,", " BWATCH "):
        assert verbose_overrides(raw) == frozenset({"bwatch"})
    assert verbose_overrides(",, ") == frozenset()
    monkeypatch.setenv("VERBOSE_LOGGING_OVERRIDES", "bwatch,core,panels")
    assert verbose_overrides() == frozenset({"bwatch", "core", "panels"})
    assert verbose_overrides("bwatch,nope") == frozenset({"bwatch", "nope"})
    monkeypatch.delenv("VERBOSE_LOGGING_OVERRIDES")
    assert verbose_overrides() == frozenset()


def test_from_env_defaults_to_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from paths import runtime_dir

    for name in (
        "DIR_DATA",
        "DIR_BACKUPS",
        "PATH_CONFIG",
        "PATH_LANG",
        "PATH_DB",
        "PATH_AUDIT",
    ):
        monkeypatch.delenv(name, raising=False)
    root = Path(__file__).resolve().parents[2]
    data = root / "data"
    assert AppPaths.from_env() == AppPaths(
        data=data,
        backups=data / "backup",
        config=data / "config.json",
        language=root / "lang.jsonc",
        database=data / "state.sqlite3",
        audit=data / "audit.jsonl",
        primary_lock=runtime_dir(data) / ".primary.lock",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("compiled", [False, True])
async def test_factory_schema_verbose_and_browser_assets(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
    compiled: bool,
) -> None:
    from async_core_helpers import PanelTransport
    from helpers import FakePanel

    from paths import bundled_root

    monkeypatch.setattr("app.compiled", lambda: compiled)
    monkeypatch.setenv("VERBOSE_LOGGING_OVERRIDES", "core,panels")
    runtime = await create_application(
        paths,
        AppOptions(
            start_background=False,
            limiter=AsyncRateLimiter("redis://test", client=OpenRedis()),
            panel_transport_factory=lambda: PanelTransport(FakePanel()),
        ),
    )
    try:
        deps = runtime.dependencies
        assert deps.cfg._require_store().schema_path == (
            str((bundled_root() / "config.schema.json").resolve()) if compiled else None
        )
        assert deps.subscription.res.verbose is True
        assert deps.panels
        assert all(panel.verbose for panel in deps.panels)
        assert paths.audit.exists()
        client = runtime.app.test_client()
        assert (await client.get("/sub/privapi/api/health")).status_code == 401
        token = "a" * 40
        await deps.db.create_user(
            username="alice",
            uuid="01234567-89ab-cdef-0123-456789abcdef",
            token=token,
            fingerprint="chrome",
            displayname="Alice",
        )
        response = await client.get(
            f"/sub?token={token}&lang=en",
            headers={"User-Agent": "Mozilla/5.0 Chrome/122.0.0.0"},
        )
        assert response.status_code == 403
        assert response.mimetype == "text/html"
        body = await response.get_data()
        assert isinstance(body, bytes)
        assert b"url('/sub/fonts/outfit-latin.woff2')" in body
        for part in (b"fonts.googleapis", b"fonts.gstatic", b"__FONT_BASE__", b"/* __FONTS__ */"):
            assert part not in body
    finally:
        await runtime.stop()


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    root = Path(__file__).resolve().parents[2]
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.json"
    document = json.loads((root / "docs/EXAMPLE.config.json").read_text())
    document["$schema"] = str(root / "config.schema.json")
    document["bot"]["token"] = ""
    config.write_text(json.dumps(document))
    return AppPaths(
        data=data,
        backups=data / "backup",
        config=config,
        language=root / "lang.jsonc",
        database=data / "state.sqlite3",
        audit=data / "audit.jsonl",
        primary_lock=data / ".primary.lock",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_optional_discord_shares_runtime(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    from async_core_helpers import PanelTransport
    from helpers import FakePanel

    document = json.loads(paths.config.read_text())
    document["discord"] = {"token": "token" if enabled else "", "whitelist": [7]}
    paths.config.write_text(json.dumps(document))
    logged_in: list[str] = []

    async def login(self: SharedDiscordClient, token: str) -> None:
        del self
        logged_in.append(token)

    async def connect(self: SharedDiscordClient, *, reconnect: bool = True) -> None:
        del self, reconnect
        await asyncio.Event().wait()

    monkeypatch.setattr(SharedDiscordClient, "login", login)
    monkeypatch.setattr(SharedDiscordClient, "connect", connect)
    runtime = await create_application(paths, AppOptions(
        start_background=False,
        limiter=AsyncRateLimiter("redis://test", client=OpenRedis()),
        panel_transport_factory=lambda: PanelTransport(FakePanel()),
    ))
    bot = runtime.dependencies.discord_bot
    try:
        assert (bot is not None) == enabled
        assert logged_in == (["token"] if enabled else [])
        if bot is not None:
            assert bot.db is runtime.dependencies.db
            assert bot.sub is runtime.dependencies.subscription
            assert bot.runner is not None
    finally:
        await runtime.stop()
    if bot is not None:
        assert bot.runner is None
        assert bot.client.is_closed()


@pytest.mark.asyncio
async def test_factory_routes_and_idempotent_stop(paths: AppPaths) -> None:
    from async_core_helpers import PanelTransport
    from helpers import FakePanel

    runtime = await create_application(
        paths,
        AppOptions(
            start_background=False,
            limiter=AsyncRateLimiter("redis://test", client=OpenRedis()),
            panel_transport_factory=lambda: PanelTransport(FakePanel()),
        ),
    )
    try:
        assert runtime.ready
        assert runtime.app.extensions["runtime"] is runtime
        assert paths.database.exists()
        assert paths.primary_lock.exists()
        assert (await runtime.app.test_client().get("/sub")).status_code == 401
        assert any(rule.rule == "/sub" for rule in runtime.app.url_map.iter_rules())
        with pytest.raises(RuntimeError, match="primary lock"):
            await create_application(
                paths,
                AppOptions(
                    start_background=False,
                    limiter=AsyncRateLimiter("redis://test", client=OpenRedis()),
                ),
            )
    finally:
        await runtime.stop()
    await runtime.stop()
    assert runtime.state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_factory_unwinds_failed_startup(paths: AppPaths) -> None:
    class BrokenRedis(OpenRedis):
        async def ping(self) -> bool:
            raise OSError("Redis unavailable")

    with pytest.raises(OSError, match="Redis unavailable"):
        await create_application(
            paths,
            AppOptions(limiter=AsyncRateLimiter("redis://test", client=BrokenRedis())),
        )
    # Primary lock was released by failed composition.
    from app_runtime.composition import PrimaryLock

    lock = PrimaryLock(paths.primary_lock)
    await lock.start()
    await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_factory_cleanup_continues_after_close_failures(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, cancel_close: bool,
) -> None:
    from app_runtime.composition import PrimaryLock
    from app_runtime.offload import OffloadServices

    events: list[str] = []
    close_offloads = OffloadServices.close

    class BrokenRedis(OpenRedis):
        async def ping(self) -> bool:
            raise OSError("Redis unavailable")

        async def aclose(self) -> None:
            events.append("redis")
            if cancel_close:
                raise asyncio.CancelledError("Redis close cancelled")
            raise ValueError("Redis close failed")

    async def broken_offloads_close(self: OffloadServices) -> None:
        await close_offloads(self)
        events.append("offloads")
        raise OSError("offloads close failed")

    monkeypatch.setattr(OffloadServices, "close", broken_offloads_close)
    limiter = AsyncRateLimiter("redis://test", client=BrokenRedis())
    limiter._owned = True
    with pytest.raises(BaseExceptionGroup, match="application startup cleanup failed") as caught:
        await create_application(
            paths,
            AppOptions(
                start_background=False,
                limiter=limiter,
            ),
        )
    assert events == ["redis", "offloads"]
    errors = caught.value.exceptions
    assert len(errors) == 2
    assert isinstance(errors[0], asyncio.CancelledError if cancel_close else ValueError)
    assert errors[0].__notes__ == ["closing startup resource: Redis"]
    assert errors[1].__notes__ == ["closing startup offloads"]
    assert isinstance(caught.value.__context__, OSError)
    lock = PrimaryLock(paths.primary_lock)
    await lock.start()
    await lock.close()


@pytest.mark.asyncio
async def test_factory_retains_cleanup_after_repeated_cancellation(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import app as application
    from app_runtime.composition import PrimaryLock
    from app_runtime.offload import OffloadServices

    starting = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    offloads_closed = asyncio.Event()
    close_offloads = OffloadServices.close

    class BlockingRedis(OpenRedis):
        async def ping(self) -> bool:
            starting.set()
            await asyncio.Event().wait()
            return True

        async def aclose(self) -> None:
            closing.set()
            await release.wait()
            raise OSError("detached Redis close failed")

    async def record_offloads_close(self: OffloadServices) -> None:
        await close_offloads(self)
        offloads_closed.set()

    monkeypatch.setattr(OffloadServices, "close", record_offloads_close)
    limiter = AsyncRateLimiter("redis://test", client=BlockingRedis())
    limiter._owned = True
    caller = asyncio.create_task(create_application(
        paths,
        AppOptions(
            start_background=False,
            limiter=limiter,
        ),
    ))
    try:
        await asyncio.wait_for(starting.wait(), timeout=5)
        caller.cancel()
        await asyncio.wait_for(closing.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert application._startup_cleanup_tasks.active_count == 1
        assert not offloads_closed.is_set()
        cleanup = tuple(application._startup_cleanup_tasks._tasks)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*cleanup, return_exceptions=True), timeout=5,
        )
        assert isinstance(results[0], ExceptionGroup)
        assert offloads_closed.is_set()
        assert application._startup_cleanup_tasks.active_count == 0
        assert "application:startup-unwind failed" in caplog.text
        lock = PrimaryLock(paths.primary_lock)
        await lock.start()
        await lock.close()
    finally:
        release.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await asyncio.gather(
            *application._startup_cleanup_tasks._tasks, return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_factory_optional_async_bots_and_notice_wiring(
    paths: AppPaths,
    enabled: bool,
) -> None:
    from unittest.mock import AsyncMock, patch

    from async_core_helpers import PanelTransport
    from helpers import FakePanel

    document = json.loads(paths.config.read_text())
    document["bot"]["token"] = "123:test"
    document["publicbot"] = {"token": "456:test"}
    paths.config.write_text(json.dumps(document))

    async def polling() -> None:
        await asyncio.Event().wait()

    with patch(
        "bots.polling.TelegramPollingMixin._poll_forever",
        new=AsyncMock(side_effect=polling),
    ):
        runtime = await create_application(
            paths,
            AppOptions(
                start_background=False,
                start_bots=enabled,
                limiter=AsyncRateLimiter("redis://test", client=OpenRedis()),
                panel_transport_factory=lambda: PanelTransport(FakePanel()),
            ),
        )
        try:
            dependencies = runtime.dependencies
            assert (dependencies.admin_bot is not None) is enabled
            assert (dependencies.public_bot is not None) is enabled
            assert (dependencies.watcher.notifier is not None) is enabled
            assert (dependencies.watcher.alert is not None) is enabled
        finally:
            await runtime.stop()
        if dependencies.public_bot is not None:
            assert dependencies.public_bot._handlers.active_count == 0
        if dependencies.admin_bot is not None:
            assert dependencies.admin_bot._handlers.active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peer", "forwarded", "expected", "status"),
    [
        ("127.0.0.1", "203.0.113.5", "203.0.113.5", 200),
        ("::1", "203.0.113.5", "203.0.113.5", 200),
        ("10.0.0.1", "127.0.0.1", "", 400),
    ],
)
async def test_proxy_guard_before_forwarding(
    peer: str,
    forwarded: str,
    expected: str,
    status: int,
) -> None:
    app = create_api_application(require_proxy=True)

    @app.route("/remote")
    async def remote() -> str:
        return request.remote_addr or ""

    response = await app.test_client().get(
        "/remote",
        headers={"X-Forwarded-For": forwarded},
        scope_base={"client": (peer, 1234)},
    )
    assert response.status_code == status
    if status == 200:
        assert await response.get_data(as_text=True) == expected


@pytest.mark.asyncio
async def test_untrusted_peer_cannot_spoof_forwarded_headers() -> None:
    app = create_api_application()

    @app.route("/remote")
    async def remote() -> str:
        return f"{request.remote_addr}|{request.scheme}|{request.host}"

    response = await app.test_client().get(
        "/remote",
        headers={
            "X-Forwarded-For": "203.0.113.5",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.test",
        },
        scope_base={"client": ("10.0.0.1", 1234)},
    )
    assert await response.get_data(as_text=True) == "10.0.0.1|http|localhost"


@pytest.mark.asyncio
async def test_access_logging_redacts_tokens(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    app = create_api_application(proxy_hops=0)

    @app.route("/sub")
    async def subscription() -> str:
        return "ok"

    @app.route("/api/state/polling")
    async def polling() -> str:
        return "ok"

    with caplog.at_level(5, logger="HTTP"):
        await app.test_client().get(
            "/sub?token=secret&password=hidden",
            headers={"User-Agent": "token=agentsecret"},
        )
        await app.test_client().get("/api/state/polling")
    assert "secret" not in caplog.text
    assert "hidden" not in caplog.text
    records = [record for record in caplog.records if record.name == "HTTP"]
    assert records[0].levelno == logging.DEBUG
    assert '/sub?token=... HTTP/1.1" \x1b[32m200\x1b[0m 2b "token=..."' in records[0].getMessage()
    assert records[1].levelno == 5

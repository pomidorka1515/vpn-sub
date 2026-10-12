from __future__ import annotations

import json
from pathlib import Path

import pytest
from async_api_helpers import OpenRedis
from quart import request

from api.application import create_api_application
from api.decorators.rate_limit import AsyncRateLimiter
from app import AppOptions, AppPaths, create_application, verbose_overrides
from app_runtime.lifecycle import RuntimeState


def test_verbose_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in ("bwatch", "bwatch,", ",bwatch,", " BWATCH "):
        assert verbose_overrides(raw) == frozenset({"bwatch"})
    assert verbose_overrides(",, ") == frozenset()
    monkeypatch.setenv("VERBOSE_LOGGING_OVERRIDES", "bwatch,core,panels")
    assert verbose_overrides() == frozenset({"bwatch", "core", "panels"})


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
    return AppPaths(data=data, backups=data / "backup", config=config, language=root / "lang.jsonc", database=data / "state.sqlite3", log=data / "log.jsonl", audit=data / "audit.jsonl", primary_lock=data / ".primary.lock")


@pytest.mark.asyncio
async def test_factory_routes_and_idempotent_stop(paths: AppPaths) -> None:
    from async_core_helpers import PanelTransport
    from helpers import FakePanel

    runtime = await create_application(paths, AppOptions(start_background=False, limiter=AsyncRateLimiter("redis://test", client=OpenRedis()), panel_transport_factory=lambda: PanelTransport(FakePanel())))
    try:
        assert runtime.ready
        assert runtime.app.extensions["runtime"] is runtime
        assert paths.database.exists()
        assert paths.primary_lock.exists()
        assert (await runtime.app.test_client().get("/sub")).status_code == 401
        assert any(rule.rule == "/sub" for rule in runtime.app.url_map.iter_rules())
        with pytest.raises(RuntimeError, match="primary lock"):
            await create_application(paths, AppOptions(start_background=False, limiter=AsyncRateLimiter("redis://test", client=OpenRedis())))
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
        await create_application(paths, AppOptions(limiter=AsyncRateLimiter("redis://test", client=BrokenRedis())))
    # Primary lock was released by failed composition.
    from app_runtime.composition import PrimaryLock

    lock = PrimaryLock(paths.primary_lock)
    await lock.start()
    await lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("peer", "forwarded", "expected", "status"), [("127.0.0.1", "203.0.113.5", "203.0.113.5", 200), ("::1", "203.0.113.5", "203.0.113.5", 200), ("10.0.0.1", "127.0.0.1", "", 400)])
async def test_proxy_guard_before_forwarding(peer: str, forwarded: str, expected: str, status: int) -> None:
    app = create_api_application(require_proxy=True)

    @app.route("/remote")
    async def remote() -> str:
        return request.remote_addr or ""

    response = await app.test_client().get("/remote", headers={"X-Forwarded-For": forwarded}, scope_base={"client": (peer, 1234)})
    assert response.status_code == status
    if status == 200:
        assert await response.get_data(as_text=True) == expected


@pytest.mark.asyncio
async def test_untrusted_peer_cannot_spoof_forwarded_headers() -> None:
    app = create_api_application()

    @app.route("/remote")
    async def remote() -> str:
        return f"{request.remote_addr}|{request.scheme}|{request.host}"

    response = await app.test_client().get("/remote", headers={"X-Forwarded-For": "203.0.113.5", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.test"}, scope_base={"client": ("10.0.0.1", 1234)})
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

    with caplog.at_level(5, logger="http.access"):
        await app.test_client().get("/sub?token=secret&password=hidden", headers={"User-Agent": "token=agentsecret"})
        await app.test_client().get("/api/state/polling")
    assert "secret" not in caplog.text
    assert "hidden" not in caplog.text
    records = [record for record in caplog.records if record.name == "http.access"]
    assert records[0].levelno == logging.DEBUG
    assert records[1].levelno == 5

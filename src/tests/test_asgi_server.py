from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import pytest

from api.application import create_api_application
from asgi import create_asgi_application
from loggers.level import TRACE
from serve import server_config


def test_server_settings_and_environment_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_BIND", raising=False)
    assert server_config().bind == ["127.0.0.1:5550"]
    monkeypatch.setenv("APP_BIND", "127.0.0.1:4321")
    config = server_config()
    assert config.bind == ["127.0.0.1:4321"]
    assert config.workers == 1
    assert config.accesslog is None
    assert config.graceful_timeout + config.shutdown_timeout < 90
    monkeypatch.delenv("LOGLEVEL_HTTP", raising=False)
    monkeypatch.setenv("LOGLEVEL_HTTP", "TRACE")
    create_api_application()
    assert logging.getLogger("http.access").level == TRACE
    monkeypatch.setenv("LOGLEVEL_HTTP", "WARNING")
    create_api_application()
    assert logging.getLogger("http.access").level == logging.WARNING


@pytest.mark.asyncio
async def test_asgi_readiness_and_lifespan() -> None:
    from unittest.mock import AsyncMock

    from app import Application, AppOptions

    runtime = AsyncMock(spec=Application)
    runtime.ready = True

    async def factory(**kwargs: object) -> Application:
        return runtime

    application = create_asgi_application(AppOptions(require_proxy=False), factory=factory)

    @application.route("/smoke")
    async def smoke() -> str:
        return "ready"

    assert (await application.test_client().get("/smoke")).status_code == 503
    async with application.test_app():
        assert (await application.test_client().get("/smoke")).status_code == 200
        runtime.ready = False
        assert (await application.test_client().get("/smoke")).status_code == 503
    runtime.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["programmatic", "checkout"])
async def test_real_server_sigterm(tmp_path: Path, entrypoint: str) -> None:
    # Controlled lifespan dependency; no production config, Redis or bot tokens.
    source = """from __future__ import annotations
from pathlib import Path
from app_runtime.lifecycle import Runtime
from asgi import create_asgi_application

class SmokeRuntime(Runtime[None]):
    async def stop(self) -> None:
        await super().stop()
        Path(__file__).with_suffix(".closed").write_text("closed")

async def factory(**kwargs):
    runtime = SmokeRuntime(None)
    await runtime.start()
    return runtime

app = create_asgi_application(factory=factory)
@app.route("/smoke")
async def smoke():
    return "ready"
"""
    (tmp_path / "smoke.py").write_text(source)
    # Reserve a loopback ephemeral port without depending on a fixed listener.
    import socket

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=f"{tmp_path}:{Path.cwd() / 'src'}", APP_BIND=f"127.0.0.1:{port}")
    command = (
        [sys.executable, "-m", "hypercorn", "--config", "python:serve", "smoke:app"]
        if entrypoint == "checkout"
        else [sys.executable, "-c", "import asyncio, asgi, smoke, main; asgi.app = smoke.app; asyncio.run(main.serve_application())"]
    )
    process = await asyncio.create_subprocess_exec(
        *command, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(15):
            while True:
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    break
                except OSError:
                    if process.returncode is not None:
                        output, _ = await process.communicate()
                        pytest.fail(output.decode())
                    await asyncio.sleep(0.05)
            writer.write(b"GET /smoke HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert b"200" in response.split(b"\r\n", 1)[0]
            assert response.endswith(b"ready")
            process.send_signal(signal.SIGTERM)
            output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()
        assert (tmp_path / "smoke.closed").read_text() == "closed"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

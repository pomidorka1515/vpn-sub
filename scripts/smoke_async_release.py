"""Exercise the real serving entrypoint against disposable loopback dependencies.

Use --binary for Nuitka acceptance or --checkout for the same runtime in CI.
No production state, panel, Redis instance or Telegram credentials are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sqlite3
import sys
import xml.etree.ElementTree as ET  # ruff: ignore[suspicious-xml-etree-import] - local compiler output
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from collections.abc import Sequence

    from config import JsonDict

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "smoke-panel-token-not-a-production-secret"  # ruff: ignore[hardcoded-password-string] - disposable fixture


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def stop(process: asyncio.subprocess.Process, deadline: float = 90) -> None:
    if process.returncode is None:
        process.send_signal(signal.SIGTERM)
    try:
        async with asyncio.timeout(deadline):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise


def check_exit(process: asyncio.subprocess.Process) -> None:
    if process.returncode != 0:
        raise RuntimeError(f"service exit status: {process.returncode}")


async def smoke(command: Sequence[str], directory: Path, *, checkout: bool) -> None:
    panel_requests: list[str] = []

    async def panel(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized
        panel_requests.append(request.path)
        if request.path == "/panel/api/server/status":
            return web.json_response(
                {
                    "success": True,
                    "msg": "",
                    "obj": {
                        "cpu": 0,
                        "cpuCores": 1,
                        "logicalPro": 1,
                        "cpuSpeedMhz": 1000,
                        "mem": {"current": 0, "total": 1024},
                        "swap": {"current": 0, "total": 1024},
                        "disk": {"current": 0, "total": 1024},
                        "xray": {"state": "running", "errorMsg": "", "version": "smoke"},
                        "uptime": 1,
                        "loads": [0.0],
                        "tcpCount": 0,
                        "udpCount": 0,
                        "netIO": {"up": 0, "down": 0},
                        "netTraffic": {"sent": 0, "recv": 0},
                        "publicIP": {"ipv4": "127.0.0.1", "ipv6": "::1"},
                        "appStats": {"threads": 1, "mem": 0, "uptime": 1},
                    },
                },
            )
        if request.path not in {"/panel/api/clients/list", "/panel/api/inbounds/list"}:
            raise web.HTTPNotFound
        return web.json_response({"success": True, "msg": "", "obj": []})

    fake = web.Application()
    fake.router.add_get("/{path:.*}", panel)
    runner = web.AppRunner(fake)
    await runner.setup()
    panel_port, redis_port, http_port = free_port(), free_port(), free_port()
    await web.TCPSite(runner, "127.0.0.1", panel_port).start()
    redis: asyncio.subprocess.Process | None = None
    service: asyncio.subprocess.Process | None = None
    try:
        redis = await asyncio.create_subprocess_exec(
            "redis-server",
            "--bind",
            "127.0.0.1",
            "--port",
            str(redis_port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(directory),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(10):
            while True:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", redis_port)
                except OSError:
                    if redis.returncode is not None:
                        raise RuntimeError("test Redis exited before startup") from None
                    await asyncio.sleep(0.05)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break
        config = cast("JsonDict", json.loads((ROOT / "docs/EXAMPLE.config.json").read_text()))
        config.pop("$schema", None)
        config.pop("bot", None)
        config.pop("publicbot", None)
        config["redis"] = {"url": f"redis://127.0.0.1:{redis_port}/0"}
        config["3xui"] = {
            "smoke": {
                "name": "smoke",
                "address": "127.0.0.1",
                "port": str(panel_port),
                "uri": "",
                "token": TOKEN,
                "https": False,
                "whitelist": False,
                "inbounds_list": [],
                "mode": "blacklist",
            },
        }
        data = directory / "state"
        data.mkdir()
        (data / "config.json").write_text(json.dumps(config))
        # Clear inherited path/serving overrides; compiled language/templates/schema
        # must be located inside the payload, even with an unrelated working dir.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PATH_", "DIR_", "GUNICORN_", "LOGLEVEL", "PYTHON"))
            and key not in {"APP_BIND", "REQUIRE_PROXY", "VERBOSE_LOGGING_OVERRIDES"}
        }
        env.update(DIR_DATA=str(data), APP_BIND=f"127.0.0.1:{http_port}", REQUIRE_PROXY="0")
        if checkout:
            env["PYTHONPATH"] = str(ROOT / "src")
        log_path = directory / "service.log"
        with log_path.open("wb") as output:
            service = await asyncio.create_subprocess_exec(
                *command,
                cwd=directory,
                env=env,
                stdout=output,
                stderr=output,
            )
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as client:
                    url = f"http://127.0.0.1:{http_port}/sub/admin/session"
                    async with asyncio.timeout(120):
                        while True:
                            if service.returncode is not None:
                                raise RuntimeError("service exited before readiness")
                            try:
                                async with client.post(
                                    url,
                                    json={
                                        "username": "username",
                                        "password": "password",
                                    },
                                ) as response:
                                    if response.status == 200:
                                        payload = await response.json()
                                        if not payload.get("success"):
                                            raise RuntimeError(f"login failed: {payload}")
                                        cookie = response.cookies["admin_ui"].value
                                        break
                            except aiohttp.ClientConnectionError:
                                pass
                            await asyncio.sleep(0.1)
                    async with client.get(
                        f"http://127.0.0.1:{http_port}/sub/admin",
                        cookies={"admin_ui": cookie},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200 or "<html" not in (await response.text()).lower():
                            raise RuntimeError("bundled admin template did not render")
                await stop(service)
                check_exit(service)
                if "/panel/api/clients/list" not in panel_requests:
                    raise RuntimeError("watcher did not seed against the test panel")
                with sqlite3.connect(data / "state.sqlite3") as db:
                    if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise RuntimeError("database integrity check failed")
                # Restart against the same state: verifies lock release, recovery,
                # resource reopening and an additional orderly shutdown.
                service = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=directory,
                    env=env,
                    stdout=output,
                    stderr=output,
                )
                async with aiohttp.ClientSession() as client, asyncio.timeout(120):
                    while True:
                        if service.returncode is not None:
                            raise RuntimeError("service could not restart")
                        try:
                            async with client.get(
                                f"http://127.0.0.1:{http_port}/sub/admin/login",
                            ) as response:
                                if response.status == 200:
                                    break
                        except aiohttp.ClientConnectionError:
                            pass
                        await asyncio.sleep(0.1)
                await stop(service)
                check_exit(service)
            except Exception:
                print(log_path.read_text(errors="replace"), file=sys.stderr)
                raise
    finally:
        if service is not None and service.returncode is None:
            await stop(service)
        if redis is not None:
            await stop(redis, 10)
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--binary", type=Path)
    group.add_argument("--checkout", action="store_true")
    parser.add_argument("--report", type=Path, help="Verify async modules in a Nuitka XML report")
    args = parser.parse_args()
    if args.report is not None:
        report = ET.parse(args.report)  # ruff: ignore[suspicious-xml-element-tree-usage] - local compiler output
        modules = {module.attrib["name"] for module in report.iter("module")}
        required = {
            "asgi",
            "quart",
            "hypercorn.asyncio",
            "aiohttp",
            "aiosqlite",
            "redis.asyncio",
            "telebot.async_telebot",
            "telebot.asyncio_helper",
        }
        if missing := required - modules:
            raise RuntimeError(f"Nuitka report missing async modules: {sorted(missing)}")
    command = [sys.executable, "-m", "main"] if args.checkout else [str(args.binary.resolve())]
    with TemporaryDirectory(prefix="vpn-sub-smoke-") as temporary:
        asyncio.run(smoke(command, Path(temporary), checkout=args.checkout))
    print("async release smoke passed: panel/Redis/SQLite/templates, SIGTERM and restart")


if __name__ == "__main__":
    main()

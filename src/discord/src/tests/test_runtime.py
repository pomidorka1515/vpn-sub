from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from discord_helpers import LANG_PATH


@pytest.fixture
def discord_env(tmp_path: Path) -> Iterator[Path]:
    config = tmp_path / "discord.json"
    config.write_text(
        json.dumps({"public": {"token": ""}, "private": {"whitelist": [], "api_token": "t"}}),
        encoding="utf-8",
    )
    old = {
        "DIR_DATA": os.environ.get("DIR_DATA"),
        "PATH_DISCORD_CONFIG": os.environ.get("PATH_DISCORD_CONFIG"),
        "PATH_DISCORD_LANG": os.environ.get("PATH_DISCORD_LANG"),
        "PATH_DISCORD_SESSIONS": os.environ.get("PATH_DISCORD_SESSIONS"),
        "PATH_LOG": os.environ.get("PATH_LOG"),
    }
    os.environ["DIR_DATA"] = str(tmp_path)
    os.environ["PATH_DISCORD_CONFIG"] = str(config)
    os.environ["PATH_DISCORD_LANG"] = str(LANG_PATH)
    os.environ["PATH_DISCORD_SESSIONS"] = str(tmp_path / "discord-sessions.json")
    os.environ["PATH_LOG"] = str(tmp_path / "log.jsonl")
    try:
        yield tmp_path
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_admin_api_uri_defaults_to_privapi(discord_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del discord_env
    monkeypatch.delenv("SUB_API_URI", raising=False)
    from runtime import DiscordPaths

    assert DiscordPaths.from_env().api_uri == "privapi"


def test_admin_api_uri_reads_env(discord_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del discord_env
    monkeypatch.setenv("SUB_API_URI", "adminapi")
    from runtime import DiscordPaths

    assert DiscordPaths.from_env().api_uri == "adminapi"


def test_empty_public_token_fails(discord_env: Path) -> None:
    del discord_env
    from runtime import create_application
    with pytest.raises(RuntimeError):
        create_application()


def test_run_stops_when_public_connect_task_finishes(discord_env: Path) -> None:
    del discord_env
    import asyncio
    from unittest.mock import patch

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def login(self, token: str) -> None:
            del token

        async def connect(self, reconnect: bool = True) -> None:
            del reconnect
            raise RuntimeError("discord runner died")

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeBot:
        def __init__(self) -> None:
            self.stopped = False

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            self.stopped = True

    class FakeHttp:
        async def start(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeSessions:
        def close(self) -> None:
            return None

    class FakeCfg:
        def close(self) -> None:
            return None

        def __getitem__(self, key: str) -> object:
            if key == "public":
                return {"token": "tok"}
            raise KeyError(key)

    fake_client = FakeClient()
    fake_public = FakeBot()
    fake_admin = FakeBot()
    fake_http = FakeHttp()
    fake_sessions = FakeSessions()
    fake_cfg = FakeCfg()
    fake_lang = FakeCfg()
    fake_log = FakeCfg()

    def fake_create() -> object:
        from runtime import DiscordApplication

        return DiscordApplication(
            cfg=fake_cfg,  # type: ignore[arg-type]
            lang_cfg=fake_lang,  # type: ignore[arg-type]
            log_cfg=fake_log,  # type: ignore[arg-type]
            http=fake_http,  # type: ignore[arg-type]
            admin_http=fake_http,  # type: ignore[arg-type]
            sessions=fake_sessions,  # type: ignore[arg-type]
            client=fake_client,  # type: ignore[arg-type]
            public_bot=fake_public,  # type: ignore[arg-type]
            admin_bot=fake_admin,  # type: ignore[arg-type]
            _token="tok",
        )

    with patch("runtime.create_application", fake_create):
        from runtime import _run

        asyncio.run(asyncio.wait_for(_run(), timeout=2))
    assert fake_client.closed
    assert fake_public.stopped
    assert fake_admin.stopped


def test_stop_closes_sessions_when_runner_is_cancelled(discord_env: Path) -> None:
    del discord_env
    import asyncio

    from runtime import DiscordApplication

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    class FakeBot:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeHttp:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeResource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        client = FakeClient()
        public = FakeBot()
        admin = FakeBot()
        http = FakeHttp()
        admin_http = FakeHttp()
        sessions = FakeResource()
        cfg = FakeResource()
        lang = FakeResource()
        log_cfg = FakeResource()

        async def hang() -> None:
            await asyncio.Event().wait()

        app = DiscordApplication(
            cfg=cfg,  # type: ignore[arg-type]
            lang_cfg=lang,  # type: ignore[arg-type]
            log_cfg=log_cfg,  # type: ignore[arg-type]
            http=http,  # type: ignore[arg-type]
            admin_http=admin_http,  # type: ignore[arg-type]
            sessions=sessions,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            public_bot=public,  # type: ignore[arg-type]
            admin_bot=admin,  # type: ignore[arg-type]
            _token="tok",
        )
        app._runner = asyncio.create_task(hang())
        await app.stop()
        assert client.closed
        assert public.stopped
        assert admin.stopped
        assert http.closed
        assert admin_http.closed
        assert sessions.closed
        assert cfg.closed
        assert app._runner.cancelled()

    asyncio.run(scenario())


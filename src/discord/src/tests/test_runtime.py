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
        json.dumps({"public": {"token": ""}, "private": {}}),
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


def test_empty_public_token_fails(discord_env: Path) -> None:
    del discord_env
    from runtime import create_application
    with pytest.raises(RuntimeError):
        create_application()


def test_run_stops_when_public_connect_task_finishes(discord_env: Path) -> None:
    del discord_env
    import asyncio
    from unittest.mock import patch

    class FakePublic:
        def __init__(self) -> None:
            self.stopped = False
            self._runner: asyncio.Task[None] | None = None

        async def start(self) -> None:
            async def boom() -> None:
                raise RuntimeError("public runner died")

            self._runner = asyncio.create_task(boom(), name="public-discord")

        async def stop(self) -> None:
            self.stopped = True
            if self._runner is not None:
                try:
                    await self._runner
                except Exception:
                    pass

    class FakeAdmin:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

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

    fake_public = FakePublic()
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
            sessions=fake_sessions,  # type: ignore[arg-type]
            public_bot=fake_public,  # type: ignore[arg-type]
            admin_bot=FakeAdmin(),  # type: ignore[arg-type]
        )

    with patch("runtime.create_application", fake_create):
        from runtime import _run

        asyncio.run(asyncio.wait_for(_run(), timeout=2))
    assert fake_public.stopped


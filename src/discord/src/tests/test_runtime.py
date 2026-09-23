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

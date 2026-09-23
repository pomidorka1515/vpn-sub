from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from sessions import SessionStore
from webapi import ApiResult


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return tmp_path / "discord-sessions.json"


@pytest.fixture
def session_store(session_path: Path) -> Iterator[SessionStore]:
    store = SessionStore(session_path)
    yield store
    store.close()


def test_401_clears_session(session_store: SessionStore) -> None:
    session_store.set_lang("123", "en")
    session_store.set_token("123", "secret-token")
    assert session_store.logged_in("123")

    class Holder:
        sessions = session_store

        def apply(self, user_id: int, result: ApiResult) -> str | None:
            if result.status == 401:
                self.sessions.clear_token(user_id)
                return "session_expired"
            if result.status == 429:
                return "rate_limited"
            return None

    holder = Holder()
    key = holder.apply(
        123,
        ApiResult(ok=False, status=401, msg="Invalid auth token.", obj=None, raw_headers={}),
    )
    assert key == "session_expired"
    record = session_store.get("123")
    assert not record.logged_in
    assert record.lang == "en"


def test_snapshot_does_not_include_tokens(session_store: SessionStore, session_path: Path) -> None:
    session_store.set_token("99", "super-secret")
    session_store.set_lang("99", "ru")
    snap = session_store.snapshot_for_log()
    encoded = json.dumps(snap)
    assert "super-secret" not in encoded
    assert "token" not in encoded
    raw = json.loads(session_path.read_text(encoding="utf-8"))
    assert raw["99"]["token"] == "super-secret"


def test_chmod_600(session_path: Path, session_store: SessionStore) -> None:
    mode = os.stat(session_path).st_mode & 0o777
    assert mode == 0o600

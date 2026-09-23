from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from config import Config, JsonDict, JsonValue

__all__ = ["SessionRecord", "SessionStore"]


@dataclass(frozen=True, slots=True)
class SessionRecord:
    user_id: str
    token: str
    lang: str | None

    @property
    def logged_in(self) -> bool:
        return bool(self.token)


class SessionStore:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        if created:
            path.write_text("{}\n", encoding="utf-8")
        os.chmod(path, 0o600)
        self._cfg = Config(
            path=path,
            indent=4,
            read_only=False,
            strict_schema=False,
            sync_mode="data",
            isolate_commits=True,
        )
        os.chmod(path, 0o600)

    def close(self) -> None:
        self._cfg.close()
        os.chmod(self._cfg.path, 0o600)

    def _key(self, user_id: int | str) -> str:
        return str(user_id)

    def get(self, user_id: int | str) -> SessionRecord:
        key = self._key(user_id)
        raw = self._cfg.get(key)
        if not isinstance(raw, dict):
            return SessionRecord(user_id=key, token="", lang=None)
        token = raw.get("token")
        lang = raw.get("lang")
        return SessionRecord(
            user_id=key,
            token=token if isinstance(token, str) else "",
            lang=lang if lang in ("en", "ru") else None,
        )

    def logged_in(self, user_id: int | str) -> bool:
        return self.get(user_id).logged_in

    def token(self, user_id: int | str) -> str | None:
        record = self.get(user_id)
        return record.token or None

    def lang(self, user_id: int | str) -> str | None:
        return self.get(user_id).lang

    def set_lang(self, user_id: int | str, lang: str) -> None:
        if lang not in ("en", "ru"):
            return
        key = self._key(user_id)
        def _edit(data: MutableMapping[str, JsonValue]) -> None:
            current = data.get(key)
            token = ""
            if isinstance(current, dict):
                raw_token = current.get("token")
                token = raw_token if isinstance(raw_token, str) else ""
            data[key] = {"token": token, "lang": lang}
        self._cfg.mutate(_edit)
        os.chmod(self._cfg.path, 0o600)

    def set_token(self, user_id: int | str, token: str) -> None:
        key = self._key(user_id)
        def _edit(data: MutableMapping[str, JsonValue]) -> None:
            current = data.get(key)
            lang = ""
            if isinstance(current, dict):
                raw_lang = current.get("lang")
                lang = raw_lang if isinstance(raw_lang, str) else ""
            payload: JsonDict = {"token": token}
            if lang in ("en", "ru"):
                payload["lang"] = lang
            data[key] = payload
        self._cfg.mutate(_edit)
        os.chmod(self._cfg.path, 0o600)

    def clear_token(self, user_id: int | str) -> None:
        self.set_token(user_id, "")


from __future__ import annotations

import base64
import hashlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import scripts_load
from loggers import Logger
from scripts_load import load_scripts


class _Log(Logger):
    def __init__(self) -> None:
        super().__init__("scripts-test")
        self.handlers.clear()
        self.propagate = False
        self.records: list[tuple[int, str]] = []

    def handle(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


def _compiled(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("__main__")
    module.__dict__["__compiled__"] = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "__main__", module)


def _plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "__main__", ModuleType("__main__"))


def _blob(body: bytes) -> dict[str, str]:
    digest = hashlib.sha1(f"blob {len(body)}\0".encode("ascii") + body).hexdigest()
    return {
        "encoding": "base64",
        "content": base64.b64encode(body).decode("ascii"),
        "sha": digest,
    }


def _get_stub(files: dict[str, bytes]) -> Any:
    def _get(url: str, *_args: object, **_kwargs: object) -> Any:
        name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if name == "scripts":
            payload: object = [
                {"name": item, "type": "file", "sha": _blob(body)["sha"]}
                for item, body in sorted(files.items())
            ]
        else:
            payload = _blob(files[name])

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> object:
                return payload

        return _Response()

    return _get


def test_refuses_outside_a_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _plain(monkeypatch)
    called = False

    def _get(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("network")

    monkeypatch.setattr(scripts_load, "_get", _get)
    log = _Log()
    assert load_scripts(log) == 1
    assert log.records[0][1] == "load-scripts only runs from the vpn-sub binary"
    assert called is False


def test_refuses_without_a_release_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(scripts_load, "VERSION", "0.0.0")
    log = _Log()
    assert load_scripts(log) == 1
    assert "no release version" in log.records[0][1]


def test_writes_schema_and_scripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    bundled = tmp_path / "payload"
    bundled.mkdir()
    schema = bundled / "config.schema.json"
    schema.write_bytes(b'{"type":"object"}\n')
    install = tmp_path / "install"
    install.mkdir()
    (install / "config.schema.json").write_bytes(b"old\n")
    files = {
        "migrate_profiles.py": b"print('migrate')\n",
        "reconcile_clients.py": b"print('reconcile')\n",
    }
    monkeypatch.setattr(scripts_load, "VERSION", "6.0.0")
    monkeypatch.setattr(scripts_load, "program_dir", lambda: install)
    monkeypatch.setattr(scripts_load, "bundled_root", lambda: bundled)
    monkeypatch.setattr(scripts_load, "_get", _get_stub(files))
    log = _Log()
    assert load_scripts(log) == 0
    assert (install / "config.schema.json").read_bytes() == b'{"type":"object"}\n'
    assert (install / "scripts" / "migrate_profiles.py").read_bytes() == files["migrate_profiles.py"]
    assert (install / "scripts" / "reconcile_clients.py").read_bytes() == files["reconcile_clients.py"]
    assert not list(install.glob(".*.part"))


def test_digest_mismatch_does_not_write_the_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _compiled(monkeypatch)
    bundled = tmp_path / "payload"
    bundled.mkdir()
    (bundled / "config.schema.json").write_bytes(b"{}\n")
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(scripts_load, "VERSION", "6.0.0")
    monkeypatch.setattr(scripts_load, "program_dir", lambda: install)
    monkeypatch.setattr(scripts_load, "bundled_root", lambda: bundled)

    def _get(url: str, *_args: object, **_kwargs: object) -> Any:
        name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if name == "scripts":
            payload: object = [
                {"name": "migrate_profiles.py", "type": "file", "sha": "a" * 40},
                {"name": "reconcile_clients.py", "type": "dir"},
                {"name": "../escape.py", "type": "file"},
            ]
        else:
            payload = {
                "encoding": "base64",
                "content": base64.b64encode(b"nope\n").decode("ascii"),
                "sha": "0" * 40,
            }

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> object:
                return payload

        return _Response()

    monkeypatch.setattr(scripts_load, "_get", _get)
    log = _Log()
    assert load_scripts(log) == 1
    assert (install / "config.schema.json").read_bytes() == b"{}\n"
    assert not (install / "scripts" / "migrate_profiles.py").exists()
    assert any("download failed" in message for _level, message in log.records)

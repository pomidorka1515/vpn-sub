from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Protocol, Self, TypedDict

import pytest

from cli import updater
from cli.updater import notice, update
from loggers import Logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class ReleaseAsset(TypedDict):
    name: str
    browser_download_url: str
    size: int
    digest: str


class ReleasePayload(TypedDict):
    tag_name: str
    draft: bool
    prerelease: bool
    body: str
    html_url: str
    assets: list[ReleaseAsset]


class StubResponse(Protocol):
    def raise_for_status(self) -> None: ...
    def json(self) -> object: ...
    def iter_content(self, chunk_size: int) -> tuple[bytes, ...]: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *_args: object) -> None: ...


class GetStub(Protocol):
    def __call__(self, *_args: object, **_kwargs: object) -> StubResponse: ...


class _Log(Logger):
    def __init__(self) -> None:
        super().__init__("updater-test")
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


def _release(tag: str = "v9.0.0", assets: tuple[str, ...] = ("vpn-sub", "vpn-sub-discord")) -> ReleasePayload:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "body": "notes",
        "html_url": f"https://github.com/pomidorka1515/vpn-sub/releases/tag/{tag}",
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://example.test/{name}",
                "size": 4,
                "digest": "sha256:" + hashlib.sha256(b"new\n").hexdigest(),
            }
            for name in assets
        ],
    }


def _response(payload: object, chunks: tuple[bytes, ...] = (b"new\n",)) -> StubResponse:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return payload

        def iter_content(self, chunk_size: int) -> tuple[bytes, ...]:
            return chunks

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    return _Response()


def _get_stub(payload: object, chunks: tuple[bytes, ...] = (b"new\n",)) -> GetStub:
    def _get(*_args: object, **_kwargs: object) -> StubResponse:
        return _response(payload, chunks)

    return _get


def _bool_stub(value: bool) -> Callable[[object], bool]:
    def _answer(*_args: object) -> bool:
        return value

    return _answer


def _empty_stub() -> Callable[[object, object], list[object]]:
    def _empty(*_args: object) -> list[object]:
        return []

    return _empty



def _units_stub() -> Callable[[object], list[tuple[str, int, str]]]:
    def _units(*_args: object) -> list[tuple[str, int, str]]:
        return [("vpn-sub.service", 10, "vpn-sub")]

    return _units


def _processes_stub() -> Callable[[object], list[tuple[int, str]]]:
    def _processes(*_args: object) -> list[tuple[int, str]]:
        return [(10, "vpn-sub"), (11, "vpn-sub-discord")]

    return _processes


def _next_stub(answers: Iterator[bool]) -> Callable[[object], bool]:
    def _answer(*_args: object) -> bool:
        return next(answers)

    return _answer


def _alive_stub() -> Callable[[object, object], list[updater.Running]]:
    def _running(*_args: object) -> list[updater.Running]:
        return [updater.Running("vpn-sub", os.getpid(), "vpn-sub.service")]

    return _running


def test_notice_does_nothing_outside_a_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _plain(monkeypatch)
    called = False

    def _get(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("network")

    monkeypatch.setattr(updater, "_get", _get)
    log = _Log()
    notice(log)
    assert log.records == []
    assert called is False


def test_update_refuses_outside_a_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _plain(monkeypatch)
    log = _Log()
    assert update(log) == 1
    assert log.records[0][1] == "updater only runs from the vpn-sub binary"


def test_notice_logs_only_a_newer_release(monkeypatch: pytest.MonkeyPatch) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_get", _get_stub(_release("v1.2.0")))
    log = _Log()
    notice(log)
    assert log.records == [(logging.INFO, "update available: 1.0.0 -> v1.2.0 (vpn-sub --update)")]


def test_notice_is_quiet_when_current(monkeypatch: pytest.MonkeyPatch) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "VERSION", "9.0.0")
    monkeypatch.setattr(updater, "_get", _get_stub(_release()))
    log = _Log()
    notice(log)
    assert log.records == []


def test_update_refuses_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "_interactive", lambda: False)
    log = _Log()
    assert update(log) == 2
    assert "terminal" in log.records[0][1]


def test_update_cancel_does_not_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(False))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_get", _get_stub(_release()))
    log = _Log()
    assert update(log) == 0
    assert binary.read_bytes() == b"old\n"


def test_digest_mismatch_does_not_replace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    payload = _release(assets=("vpn-sub",))
    payload["assets"][0]["digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_running", _empty_stub())
    monkeypatch.setattr(updater, "_get", _get_stub(payload))
    log = _Log()
    assert update(log) == 1
    assert binary.read_bytes() == b"old\n"
    assert not (tmp_path / "vpn-sub.bak").exists()


def test_second_replace_restores_the_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    first = tmp_path / "vpn-sub"
    second = tmp_path / "vpn-sub-discord"
    first.write_bytes(b"old-main")
    second.write_bytes(b"old-discord")
    real_replace = Path.replace

    def _replace(self: Path, target: Path) -> Path:
        if self.name == "vpn-sub-discord.new":
            raise OSError("second rename failed")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _replace)
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_running", _empty_stub())
    body = _release()
    payload = b"new-bin"
    for asset in body["assets"]:
        asset["size"] = len(payload)
        asset["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater, "_get", _get_stub(body, (payload,)))
    log = _Log()
    assert update(log) == 1
    assert first.read_bytes() == b"old-main"
    assert second.read_bytes() == b"old-discord"


def test_missing_sibling_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_running", _empty_stub())
    payload = _release(assets=("vpn-sub",))
    monkeypatch.setattr(updater, "_get", _get_stub(payload))
    log = _Log()
    assert update(log) == 0
    assert binary.read_bytes() == b"new\n"
    assert not (tmp_path / "vpn-sub-discord").exists()


def test_running_lists_unit_and_stray_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old")
    discord = tmp_path / "vpn-sub-discord"
    discord.write_bytes(b"old")
    monkeypatch.setattr(
        updater,
        "_units",
        _units_stub(),
    )
    monkeypatch.setattr(updater, "_processes", _processes_stub())
    found = updater._running(_Log(), {"vpn-sub": binary, "vpn-sub-discord": discord})
    assert found is not None
    assert [(item.pid, item.unit) for item in found] == [(10, "vpn-sub.service"), (11, None)]


def test_asset_requires_sha256() -> None:
    with pytest.raises(ValueError, match="has no sha256 digest"):
        updater._asset({"name": "vpn-sub", "browser_download_url": "https://x", "size": 1, "digest": "md5:abc"})


def test_parse_strips_release_suffix() -> None:
    assert updater._parse("v5.0.0-rc.3") == (5, 0, 0)
    assert updater._newer((5, 1), (5, 0, 0))
    assert not updater._newer((5, 0), (5, 0, 0))


def test_swap_refuses_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    target = tmp_path / "real"
    target.write_bytes(b"old")
    link = tmp_path / "vpn-sub"
    link.symlink_to(target)
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_get", _get_stub(_release(assets=("vpn-sub",))))
    log = _Log()
    assert update(log) == 1
    assert target.read_bytes() == b"old"
    assert any("symlink" in message for _level, message in log.records)


def test_alive_pid_blocks_replace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    answers = iter((True, True))
    monkeypatch.setattr(updater, "_confirm", _next_stub(answers))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(
        updater,
        "_running",
        _alive_stub(),
    )
    monkeypatch.setattr(updater, "_get", _get_stub(_release(assets=("vpn-sub",))))
    log = _Log()
    assert update(log) == 1
    assert binary.read_bytes() == b"old\n"
    assert any("make sure both services are stopped before continuing" in message for _level, message in log.records)
    assert any("still running" in message for _level, message in log.records)

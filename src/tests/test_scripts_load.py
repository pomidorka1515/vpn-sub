from __future__ import annotations

import base64
import hashlib
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Protocol

import cli.scripts as scripts_load
from cli.scripts import load_scripts

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class StubResponse(Protocol):
    def raise_for_status(self) -> None: ...
    def json(self) -> object: ...


class GetStub(Protocol):
    def __call__(self, url: str, *_args: object, **_kwargs: object) -> StubResponse: ...


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


def _get_stub(files: dict[str, bytes]) -> GetStub:
    def _get(url: str, *_args: object, **_kwargs: object) -> StubResponse:
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


def test_refuses_outside_a_binary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plain(monkeypatch)
    called = False

    def _get(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("network")

    monkeypatch.setattr(scripts_load, "_get", _get)
    assert load_scripts() == 1
    assert capsys.readouterr().out == "load-scripts only runs from the vpn-sub binary\n"
    assert called is False


def test_refuses_without_a_release_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(scripts_load, "VERSION", "0.0.0")
    assert load_scripts() == 1
    assert "no release version" in capsys.readouterr().out


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
    assert load_scripts() == 0
    assert (install / "config.schema.json").read_bytes() == b'{"type":"object"}\n'
    assert (install / "scripts" / "migrate_profiles.py").read_bytes() == files[
        "migrate_profiles.py"
    ]
    assert (install / "scripts" / "reconcile_clients.py").read_bytes() == files[
        "reconcile_clients.py"
    ]
    assert not list(install.glob(".*.part"))


def test_digest_mismatch_does_not_write_the_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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

    def _get(url: str, *_args: object, **_kwargs: object) -> StubResponse:
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
    assert load_scripts() == 1
    assert (install / "config.schema.json").read_bytes() == b"{}\n"
    assert not (install / "scripts" / "migrate_profiles.py").exists()
    assert "download failed" in capsys.readouterr().out


def test_scripts_use_full_prerelease_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scripts_load, "VERSION", "6.0.0-rc.2+build.1")
    urls: list[str] = []
    stub = _get_stub({"migrate.py": b"script\n"})

    def _get(url: str, *args: object, **kwargs: object) -> StubResponse:
        urls.append(url)
        return stub(url, *args, **kwargs)

    monkeypatch.setattr(scripts_load, "_get", _get)
    name, digest = scripts_load._listing()[0]
    assert scripts_load._script(name, digest) == b"script\n"
    assert len(urls) == 2
    assert all(url.endswith("?ref=v6.0.0-rc.2%2Bbuild.1") for url in urls)

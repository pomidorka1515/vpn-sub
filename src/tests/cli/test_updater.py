from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Protocol, Self, TypedDict

import pytest

from cli import updater
from cli.updater import notice, update

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__all__: list[str] = []
pytestmark: pytest.MarkDecorator = pytest.mark.usefixtures("glibc_host")


@pytest.fixture
def glibc_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep update tests independent of the developer machine's ABI."""
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "confstr", _value_stub("glibc 2.35"), raising=False)


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


def _compiled(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("__main__")
    module.__dict__["__compiled__"] = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "__main__", module)


def _plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "__main__", ModuleType("__main__"))


def _release(
    tag: str = "v9.0.0",
    assets: tuple[str, ...] = updater.RELEASE_BINARIES,
) -> ReleasePayload:
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
    releases: list[object] = [payload]

    def _get(*_args: object, **_kwargs: object) -> StubResponse:
        return _response(releases if isinstance(payload, dict) else payload, chunks)

    return _get


def _bool_stub(value: bool) -> Callable[[object], bool]:
    def _answer(*_args: object) -> bool:
        return value

    return _answer


def _value_stub[T](value: T) -> Callable[..., T]:
    def result(*_args: object, **_kwargs: object) -> T:
        return value

    return result


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


def test_notice_does_nothing_outside_a_binary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plain(monkeypatch)
    called = False

    def _get(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("network") from None

    monkeypatch.setattr(updater, "_get", _get)
    notice()
    assert capsys.readouterr().out == ""
    assert called is False


def test_notice_can_be_disabled_in_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _compiled(monkeypatch)
    monkeypatch.setenv("VPN_SUB_DISABLE_UPDATE_NOTICE", "1")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network") from None

    monkeypatch.setattr(updater, "_get", fail)
    notice()


def test_update_refuses_outside_a_binary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plain(monkeypatch)
    assert update() == 1
    assert capsys.readouterr().out == "updater only runs from the vpn-sub binary\n"


def test_notice_prints_only_a_newer_release(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_get", _get_stub(_release("v1.2.0")))
    notice()
    assert capsys.readouterr().out == "update available: 1.0.0 -> v1.2.0 (vpn-sub --update)\n"


def test_notice_is_quiet_when_current(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "VERSION", "9.0.0")
    monkeypatch.setattr(updater, "_get", _get_stub(_release()))
    notice()
    assert capsys.readouterr().out == ""


def test_update_refuses_without_a_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "_interactive", lambda: False)
    assert update() == 2
    assert "terminal" in capsys.readouterr().out


def test_update_cancel_does_not_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(False))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_get", _get_stub(_release()))
    assert update() == 0
    assert binary.read_bytes() == b"old\n"


def test_digest_mismatch_does_not_replace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    payload = _release(assets=("vpn-sub-amd64-glibc",))
    payload["assets"][0]["digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_running", _empty_stub())
    monkeypatch.setattr(updater, "_get", _get_stub(payload))
    assert update() == 1
    assert binary.read_bytes() == b"old\n"
    assert not (tmp_path / "vpn-sub.bak").exists()


def test_failed_replace_preserves_installed_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _compiled(monkeypatch)
    first = tmp_path / "vpn-sub"
    second = tmp_path / "vpn-sub-discord"
    first.write_bytes(b"old-main")
    second.write_bytes(b"old-discord")
    real_replace = Path.replace

    def _replace(self: Path, target: Path) -> Path:
        if self.name == "vpn-sub.new":
            raise OSError("rename failed") from None
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
    assert update() == 1
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
    payload = _release(assets=("vpn-sub-amd64-glibc",))
    monkeypatch.setattr(updater, "_get", _get_stub(payload))
    assert update() == 0
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
    found = updater._running({"vpn-sub": binary, "vpn-sub-discord": discord})
    assert found is not None
    assert [(item.pid, item.unit) for item in found] == [(10, "vpn-sub.service"), (11, None)]


def test_asset_requires_sha256() -> None:
    with pytest.raises(ValueError, match="has no sha256 digest"):
        updater._asset(
            {
                "name": "vpn-sub",
                "browser_download_url": "https://x",
                "size": 1,
                "digest": "md5:abc",
            },
        )


@pytest.mark.parametrize(
    ("release", "current", "newer"),
    [
        ("v5.0.0-rc.3", "5.0.0-rc.2", True),
        ("5.0.0-rc.10", "5.0.0-rc.9", True),
        ("5.0.0", "5.0.0-rc.3", True),
        ("5.0.0-rc.3", "5.0.0", False),
        ("5.0.0-beta", "5.0.0-alpha", True),
        ("5.0.0-rc.1", "5.0.0-beta.9", True),
        ("5.0.0-preview.2", "5.0.0-preview.1", True),
        ("5.0.0-rc.1+build.2", "5.0.0-rc.1+build.1", False),
        ("5.1", "5.0.0", True),
        ("5.0", "5.0.0", False),
    ],
)
def test_parse_preserves_suffix_and_compares_versions(
    release: str,
    current: str,
    newer: bool,
) -> None:
    parsed = updater._parse(release)
    installed = updater._parse(current)
    assert parsed is not None
    assert installed is not None
    assert parsed.text == release.removeprefix("v")
    assert updater._newer(parsed, installed) is newer


def test_latest_includes_prereleases_and_skips_drafts(monkeypatch: pytest.MonkeyPatch) -> None:
    rc = _release("v5.0.0-rc.10")
    rc["prerelease"] = True
    draft = _release("v6.0.0")
    draft["draft"] = True
    monkeypatch.setattr(
        updater,
        "_get",
        _get_stub([_release("v5.0.0-rc.9"), draft, rc, _release("v4.0.0")]),
    )
    latest = updater._latest(3)
    assert latest.tag == "v5.0.0-rc.10"
    assert latest.prerelease


def test_latest_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def _get(url: str, *_args: object, **_kwargs: object) -> StubResponse:
        urls.append(url)
        return _response(
            [_release("v1.0.0")] * 100 if url.endswith("&page=1") else [_release("v2.0.0-rc.1")],
        )

    monkeypatch.setattr(updater, "_get", _get)
    assert updater._latest(3).tag == "v2.0.0-rc.1"
    assert len(urls) == 2


def test_rc_update_warns_before_confirmation_and_replaces_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    payload = _release("v5.0.0-rc.2", assets=("vpn-sub-amd64-glibc",))
    payload["prerelease"] = True
    monkeypatch.setattr(updater, "VERSION", "5.0.0-rc.1")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_running", _empty_stub())
    monkeypatch.setattr(updater, "_get", _get_stub(payload))

    def _confirm(_question: str) -> bool:
        output = capsys.readouterr().out
        assert "!! WARNING !!" in output
        assert "NOT STABLE" in output
        return True

    monkeypatch.setattr(updater, "_confirm", _confirm)
    assert update() == 0
    assert binary.read_bytes() == b"new\n"


def test_prerelease_notice_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    monkeypatch.setattr(updater, "VERSION", "5.0.0-beta")
    monkeypatch.setattr(updater, "_get", _get_stub(_release("v5.0.0-rc.1")))
    notice()
    output = capsys.readouterr().out
    assert "5.0.0-beta -> v5.0.0-rc.1" in output
    assert "!! WARNING !!" in output


def test_swap_refuses_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    target = tmp_path / "real"
    target.write_bytes(b"old")
    link = tmp_path / "vpn-sub"
    link.symlink_to(target)
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_get", _get_stub(_release(assets=("vpn-sub-amd64-glibc",))))
    assert update() == 1
    assert target.read_bytes() == b"old"
    assert "symlink" in capsys.readouterr().out


def test_alive_pid_blocks_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    monkeypatch.setattr(updater, "_get", _get_stub(_release(assets=("vpn-sub-amd64-glibc",))))
    assert update() == 1
    assert binary.read_bytes() == b"old\n"
    output = capsys.readouterr().out
    assert "make sure the service is stopped before continuing" in output
    assert "still running" in output


@pytest.mark.parametrize(
    ("machine", "arch"),
    [("x86_64", "amd64"), ("amd64", "amd64"), ("aarch64", "aarch64"), ("arm64", "aarch64")],
)
@pytest.mark.parametrize("libc", ["glibc", "musl"])
def test_update_selects_abi_and_installs_plain_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    machine: str,
    arch: str,
    libc: str,
) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old\n")
    monkeypatch.setattr(platform, "machine", lambda: machine)
    if libc == "musl":
        monkeypatch.setattr(os, "confstr", _value_stub(None))
        maps = f"7000-8000 r-xp 0000 00:00 1 /lib/ld-musl-{machine}.so.1\n"
        monkeypatch.setattr(Path, "read_text", _value_stub(maps))
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_running", _empty_stub())
    release = _release()
    downloads: list[str] = []
    for asset in release["assets"]:
        content = asset["name"].encode()
        asset["size"] = len(content)
        asset["digest"] = "sha256:" + hashlib.sha256(content).hexdigest()

    def get(url: str, *_args: object, **_kwargs: object) -> StubResponse:
        if url.startswith(updater.RELEASES_URL):
            return _response([release])
        downloads.append(url)
        name = url.rsplit("/", 1)[1]
        return _response(None, (name.encode(),))

    monkeypatch.setattr(updater, "_get", get)
    expected = f"vpn-sub-{arch}-{libc}"
    assert update() == 0
    assert downloads == [f"https://example.test/{expected}"]
    assert binary.read_bytes() == expected.encode()
    assert (tmp_path / "vpn-sub.bak").read_bytes() == b"old\n"
    assert binary.stat().st_mode & 0o777 == 0o755
    assert sorted(path.name for path in tmp_path.iterdir()) == ["vpn-sub", "vpn-sub.bak"]


def test_musl_detection_when_confstr_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(_name: str) -> str:
        raise ValueError("unrecognized configuration name")

    monkeypatch.setattr(os, "confstr", unsupported)
    monkeypatch.setattr(
        Path, "read_text", _value_stub("/lib/ld-musl-x86_64.so.1\n"),
    )
    assert updater._platform_asset() == "vpn-sub-amd64-musl"


def test_unsupported_libc_is_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "confstr", _value_stub(None))
    monkeypatch.setattr(Path, "read_text", _value_stub("/apex/lib64/bionic/libc.so\n"))
    with pytest.raises(ValueError, match="unsupported libc"):
        updater._platform_asset()


@pytest.mark.parametrize("machine", ["armv7l", "riscv64", "i686"])
def test_unsupported_arch_is_not_guessed(monkeypatch: pytest.MonkeyPatch, machine: str) -> None:
    monkeypatch.setattr(platform, "machine", lambda: machine)
    with pytest.raises(ValueError, match="unsupported platform"):
        updater._platform_asset()


def test_missing_abi_asset_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _compiled(monkeypatch)
    binary = tmp_path / "vpn-sub"
    binary.write_bytes(b"old")
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    monkeypatch.setattr(updater, "_interactive", lambda: True)
    monkeypatch.setattr(updater, "_confirm", _bool_stub(True))
    monkeypatch.setattr(updater, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(
        updater, "_get", _get_stub(_release(assets=("vpn-sub", "vpn-sub-aarch64-glibc"))),
    )
    assert update() == 1
    assert "no asset for vpn-sub-amd64-glibc" in capsys.readouterr().out
    assert binary.read_bytes() == b"old"


def test_systemctl_without_systemd_still_checks_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", _value_stub("/usr/bin/systemctl"))
    monkeypatch.setattr(Path, "is_dir", _value_stub(False))

    def no_systemctl(*_args: object, **_kwargs: object) -> None:
        pytest.fail("must not call systemctl without a systemd manager")

    monkeypatch.setattr(subprocess, "run", no_systemctl)
    monkeypatch.setattr(updater, "_self_pids", _value_stub(set[int]()))
    monkeypatch.setattr(updater, "_processes", _processes_stub())
    found = updater._running({"vpn-sub": tmp_path / "vpn-sub"})
    assert found is not None
    assert [(item.pid, item.unit) for item in found] == [(10, None), (11, None)]


def test_systemctl_failure_with_live_systemd_is_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _value_stub("/usr/bin/systemctl"))
    monkeypatch.setattr(Path, "is_dir", _value_stub(True))
    monkeypatch.setattr(
        subprocess, "run",
        _value_stub(SimpleNamespace(returncode=1, stdout="", stderr="access denied")),
    )
    assert updater._units({}) is None

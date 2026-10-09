from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from cli.help import print_help, usage
from version import VERSION

ROOT = Path(__file__).resolve().parents[1]
DISCORD = ROOT / "discord" / "src"


def test_usage_names_the_binary_and_version() -> None:
    text = usage("vpn-sub")
    assert text.startswith("usage: vpn-sub [--help] [--probe] [--update] [--load-scripts]\n")
    assert VERSION in text
    assert "--probe" in text
    assert "--update" in text
    assert "--load-scripts" in text


def test_print_help_writes_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    print_help("vpn-sub-discord")
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: vpn-sub-discord ")
    assert captured.err == ""


def test_main_help_exits_before_boot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["vpn-sub", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: vpn-sub ")


def test_main_short_help_exits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["vpn-sub", "-h"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    assert caught.value.code == 0
    assert capsys.readouterr().out.startswith("usage: vpn-sub ")


def test_discord_help_exits_before_boot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.syspath_prepend(str(DISCORD))  # pyright: ignore[reportUnknownMemberType]  # Upstream parameter is untyped.
    monkeypatch.setattr(sys, "argv", ["vpn-sub-discord", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(DISCORD / "runtime.py"), run_name="__main__")
    assert caught.value.code == 0
    assert capsys.readouterr().out.startswith("usage: vpn-sub-discord ")

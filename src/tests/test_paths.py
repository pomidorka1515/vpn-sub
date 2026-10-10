from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

from paths import program_dir

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _compiled_main(monkeypatch: pytest.MonkeyPatch, compiled: object) -> None:
    module = ModuleType("__main__")
    module.__dict__["__compiled__"] = compiled
    monkeypatch.setitem(sys.modules, "__main__", module)


def test_program_dir_uses_cwd_outside_a_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "__main__", ModuleType("__main__"))
    monkeypatch.chdir(tmp_path)
    assert program_dir() == tmp_path


def test_program_dir_uses_argv_inside_a_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "vpn-sub"
    monkeypatch.setattr(sys, "argv", [str(binary)])
    _compiled_main(monkeypatch, SimpleNamespace())
    assert program_dir() == tmp_path


def test_program_dir_prefers_argv_over_original_argv0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    operator = tmp_path / "elsewhere"
    monkeypatch.setattr(sys, "argv", [str(install / "vpn-sub")])
    _compiled_main(monkeypatch, SimpleNamespace(original_argv0=str(operator / "vpn-sub")))
    assert program_dir() == install


def test_program_dir_ignores_stdin_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "argv", ["-"])
    _compiled_main(monkeypatch, SimpleNamespace(containing_dir=str(tmp_path)))
    assert program_dir() == tmp_path


def test_program_dir_falls_back_to_containing_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "argv", ["-"])
    _compiled_main(monkeypatch, SimpleNamespace(original_argv0=None, containing_dir=str(tmp_path)))
    assert program_dir() == tmp_path


def test_compiled_is_false_for_a_checkout() -> None:
    from paths import compiled

    assert compiled() is False


def test_compiled_reads_the_entry_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from paths import compiled

    _compiled_main(monkeypatch, SimpleNamespace())
    assert compiled() is True

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

__all__: list[str] = []

pytest.importorskip("python_minifier")

CLI: list[str] = [sys.executable, "-m", "obfuscator"]


def test_cli_excludes_build_only_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text('"documentation"\nvalue: int = 5\n')
    for directory in (
        source / "tests",
        source / "package" / "tests",
        source / "obfuscator",
        source / "obfuscator" / "renamers",
        source / "package" / "obfuscator",
        source / "__pycache__",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.py").write_text("not valid Python !!!")
    output = tmp_path / "output"
    subprocess.run(
        [*CLI, str(source), str(output), "--allowlist", "[]"],
        check=True,
    )
    assert [path.relative_to(output) for path in output.rglob("*")] == [Path("app.min.py")]


@pytest.mark.parametrize("force", [None, "-f", "--force"])
def test_cli_requires_force_to_overwrite_output(tmp_path: Path, force: str | None) -> None:
    source: Path = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("__result__ = 42\n", encoding="utf-8")
    output: Path = tmp_path / "output"
    output.mkdir()
    artifact: Path = output / "app.min.py"
    artifact.write_text("old release", encoding="utf-8")
    unrelated: Path = output / "keep.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [
            *CLI,
            str(source),
            str(output),
            "--allowlist",
            "[]",
            *([force] if force else []),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    if force is None:
        assert result.returncode == 2
        assert "use --force" in result.stderr
        assert artifact.read_text(encoding="utf-8") == "old release"
    else:
        assert result.returncode == 0, result.stderr
        namespace: dict[str, Any] = {}
        exec(  # ruff: ignore[exec-builtin] - Fixed fixture.
            artifact.read_text(encoding="utf-8"), namespace
        )
        assert namespace["__result__"] == 42


@pytest.mark.parametrize("existing_directory", [False, True])
def test_cli_force_accepts_new_and_empty_output_directories(
    tmp_path: Path, existing_directory: bool
) -> None:
    source: Path = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("__result__ = 42\n", encoding="utf-8")
    output: Path = tmp_path / "output"
    if existing_directory:
        output.mkdir()
    subprocess.run(
        [*CLI, str(source), str(output), "--allowlist", "[]", "-f"],
        check=True,
    )
    assert (output / "app.min.py").is_file()


@pytest.mark.parametrize("relative_output", ["source", "source/output", "."])
def test_cli_force_still_rejects_overlapping_directories(
    tmp_path: Path, relative_output: str
) -> None:
    source: Path = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("__result__ = 42\n", encoding="utf-8")
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [*CLI, str(source), str(tmp_path / relative_output), "-f"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "non-overlapping directories" in result.stderr
    assert (source / "main.py").read_text(encoding="utf-8") == "__result__ = 42\n"


def test_cli_transforms_keyword_calls_across_relative_imports(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .worker import calculate as exported\n")
    (package / "worker.py").write_text(
        "def calculate(long_parameter, *, multiplier=2): return long_parameter * multiplier\n"
    )
    (source / "main.py").write_text(
        "from package import exported as run\nprint(run(long_parameter=3, multiplier=4))\n"
    )
    output = tmp_path / "output"
    subprocess.run(
        [
            *CLI,
            str(source),
            str(output),
            "--allowlist",
            '["a:calculate:long_parameter", "a:calculate:multiplier"]',
        ],
        check=True,
    )
    result = subprocess.run(
        [sys.executable, str(output / "app.min.py")], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "12"
    function = next(
        node
        for node in ast.parse((output / "app.min.py").read_text()).body
        if isinstance(node, ast.FunctionDef)
    )
    assert isinstance(function, ast.FunctionDef)
    assert all(
        argument.arg not in {"long_parameter", "multiplier"} for argument in function.args.args
    )


def test_cli_resolves_imported_instances_and_rewrites_keywords(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text(
        "class Helper:\n    def calculate(self, long_parameter): return long_parameter * 2\n"
    )
    (source / "main.py").write_text(
        "from worker import Helper as Worker\n"
        "def run():\n"
        "    helper = Worker()\n"
        "    return helper.calculate(long_parameter=4)\n"
        "print(run())\n"
    )
    output = tmp_path / "output"
    subprocess.run(
        [
            *CLI,
            str(source),
            str(output),
            "--allowlist",
            '["a:Helper:calculate:long_parameter"]',
        ],
        check=True,
    )
    result = subprocess.run(
        [sys.executable, str(output / "app.min.py")], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "8"
    function = next(
        node
        for node in ast.walk(ast.parse((output / "app.min.py").read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "calculate"
    )
    assert function.args.args[1].arg != "long_parameter"
    assert "long_parameter=" not in (output / "app.min.py").read_text()


@pytest.mark.parametrize("allowlist", ["[]", '["f:increment"]'])
def test_cli_allowlist_supplements_automatic_renaming(tmp_path: Path, allowlist: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text(
        "from math import sqrt as square_root\n"
        "long_counter = 1\n"
        "class Helper:\n"
        " def _calculate(self, long_parameter): return square_root(long_parameter)\n"
        "def increment():\n global long_counter\n long_counter += 1\n"
    )
    (source / "main.py").write_text(
        "import worker\n"
        "long_counter = 99\n"
        "worker.long_counter = 4\nworker.increment()\n"
        "def run():\n"
        " helper = worker.Helper()\n"
        " return helper._calculate(long_parameter=16), worker.long_counter, long_counter\n"
        "print(run())\n"
    )
    output = tmp_path / "output"
    subprocess.run(
        [*CLI, str(source), str(output), "--allowlist", allowlist],
        check=True,
    )
    result = (output / "app.min.py").read_text()
    assert "long_counter" not in result
    assert "square_root" not in result
    assert "_calculate" not in result
    assert "class Helper:" not in result
    assert ("def increment(" in result) == (allowlist == "[]")
    execution = subprocess.run(
        [sys.executable, str(output / "app.min.py")], check=True, capture_output=True, text=True
    )
    assert execution.stdout.strip() == "(4.0, 5, 99)"


@pytest.mark.parametrize("report", [False, True])
@pytest.mark.parametrize("allowlist", ["[]", '["c:Helper"]'])
def test_cli_always_warns_about_class_renaming(
    tmp_path: Path, report: bool, allowlist: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("class Helper: pass\n__result__ = Helper.__name__\n")
    output = tmp_path / "output"
    result = subprocess.run(
        [
            *CLI,
            str(source),
            str(output),
            "--allowlist",
            allowlist,
            *(["--report"] if report else []),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stderr == "Renaming classes enabled. Beware of mangled log output.\n"
    assert ("c:Helper ->" in result.stdout) == (report and allowlist != "[]")
    namespace: dict[str, Any] = {}
    exec(  # ruff: ignore[exec-builtin] - Fixed fixture.
        (output / "app.min.py").read_text(encoding="utf-8"), namespace
    )
    assert (namespace["__result__"] == "Helper") == (allowlist == "[]")


def test_cli_reports_parameter_rename_counts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text(
        "def calculate(long_parameter): return long_parameter\n"
        "def external(public_parameter): return public_parameter\n"
        "result = calculate(long_parameter=4)\n"
    )
    result = subprocess.run(
        [
            *CLI,
            str(source),
            str(tmp_path / "output"),
            "--report",
            "--allowlist",
            '["a:external:public_parameter"]',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Renamed 1 allowlisted identifiers" in result.stdout
    assert "a:external:public_parameter ->" in result.stdout
    assert "Renamed 1/2 parameters automatically" in result.stdout

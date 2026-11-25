from __future__ import annotations

import ast
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from obfuscator.parallel import default_jobs
from obfuscator.pipeline import obfuscate_modules
from obfuscator.sql import minify_sql_literals

__all__: list[str] = []


@pytest.mark.parametrize(("cpus", "expected"), [(None, 1), (1, 1), (2, 1), (7, 3), (16, 8)])
def test_default_jobs(cpus: int | None, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "process_cpu_count", lambda: cpus)
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    assert default_jobs() == expected


def test_parallel_pipeline_matches_serial() -> None:
    sources: dict[Path, str] = {
        Path("contracts/__init__.py"): "from typing import Protocol\nclass Root(Protocol): pass\n",
        Path("contracts/child.py"): (
            "from . import Root as Parent\n"
            "from .child import Child as LocalAlias\n"
            "class Child(Parent): pass\n"
            "class Grandchild(LocalAlias): pass\n"
        ),
        Path("worker.py"): """
from contracts.child import Grandchild
class Leaf(Grandchild): pass
class Box[T]:
    def __init__(self, value: T): self.value = value
    def _get(self): return self.value
def helper(value): return Box(value)._get()
""",
        Path("main.py"): """
from worker import helper
__result__ = helper(value=4)
__query__ = " SELECT a, b FROM t WHERE id = ? "
""",
    }
    serial = obfuscate_modules(sources, ["f:helper"], jobs=1)
    parallel = obfuscate_modules(sources, ["f:helper"], jobs=2)
    assert parallel == serial
    assert "Grandchild" not in parallel.source
    assert "Leaf" not in parallel.source
    namespace: dict[str, Any] = {}
    exec(parallel.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 4
    assert namespace["__query__"] == " SELECT a,b FROM t WHERE id=? "


def test_parallel_sql_matches_serial() -> None:
    source = """
query = " SELECT a, b FROM t "
duplicate = " SELECT a, b FROM t "
dynamic = f"SELECT 1 + 2 {query}"
raw = b"SELECT 1 + 2"
other = "not SQL"
"""
    serial = ast.parse(source)
    parallel = ast.parse(source)
    # Folding/linking may retain shared AST nodes: apply replacements using
    # original values even when a literal is encountered more than once.
    serial.body.append(serial.body[0])
    parallel.body.append(parallel.body[0])
    minify_sql_literals(serial)
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        minify_sql_literals(parallel, executor=pool)
    assert ast.dump(parallel) == ast.dump(serial)


@pytest.mark.parametrize(("modules", "jobs"), [(2, 1), (1, 20)])
def test_serial_and_single_module_do_not_start_workers(
    modules: int, jobs: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_pool(*args: object, **kwargs: object) -> None:
        pytest.fail("serial execution must not start a process pool")

    monkeypatch.setattr("obfuscator.pipeline.ProcessPoolExecutor", unexpected_pool)
    sources = {Path(f"module_{index}.py"): "pass" for index in range(modules)}
    obfuscate_modules(sources, [], jobs=jobs)


@pytest.mark.parametrize("jobs", [0, -1])
def test_invalid_jobs(jobs: int) -> None:
    with pytest.raises(ValueError, match="jobs must be at least 1"):
        obfuscate_modules({Path("main.py"): "pass"}, [], jobs=jobs)


def test_parallel_parse_error_preserves_filename() -> None:
    with pytest.raises(SyntaxError) as caught:
        obfuscate_modules({Path("bad.py"): "def broken(", Path("main.py"): "pass"}, [], jobs=2)
    assert caught.value.filename == "bad.py"


def test_cli_jobs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def helper(value): return value + 1\n")
    (source / "main.py").write_text("from worker import helper\n__result__ = helper(value=4)\n")
    outputs: list[str] = []
    for jobs in (1, 2):
        output = tmp_path / f"output-{jobs}"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "obfuscator",
                str(source),
                str(output),
                "--allowlist",
                "[]",
                "--jobs",
                str(jobs),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append((output / "app.min.py").read_text())
    assert outputs[0] == outputs[1]
    rejected = subprocess.run(
        [sys.executable, "-m", "obfuscator", str(source), str(tmp_path / "invalid"), "--jobs", "0"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert "--jobs must be at least 1" in rejected.stderr
    assert not (tmp_path / "invalid").exists()

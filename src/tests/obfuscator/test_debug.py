from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from obfuscator.debug import annotate_debug, record_attribute
from obfuscator.pipeline import obfuscate_modules

__all__: list[str] = []


def _without_comments(source: str) -> str:
    return "".join(
        line for line in source.splitlines(keepends=True) if not line.lstrip().startswith("#")
    )


def test_debug_tracks_automatic_and_allowlisted_names_without_changing_code() -> None:
    source = """
class Record:
    def __init__(self, number): self._value = number
    def _read(self, increment): return self._value + increment
def calculate(value):
    local_variable = value * 2
    return local_variable
record = Record(number=4)
__result__ = calculate(value=record._read(increment=3))
"""
    entries = ["attr:Record:_value", "f:calculate"]
    originals = {Path("main.py"): source}
    normal = obfuscate_modules(originals, entries, jobs=1)
    debug = obfuscate_modules(originals, entries, jobs=1, debug=True)
    assert _without_comments(debug.source) == normal.source
    assert ast.dump(ast.parse(debug.source)) == ast.dump(ast.parse(normal.source))
    assert re.match(r"# app\.min\.py \d{4}-\d{2}-\d{2} \d{2}:\d{2}\n", debug.source)
    assert "# allowlist used" not in debug.source
    assert all(entry not in debug.source for entry in entries)
    for kind, original in (
        ("class", "Record"),
        ("func", "_read"),
        ("func", "calculate"),
        ("arg", "number"),
        ("arg", "increment"),
        ("arg", "value"),
        ("attr", "_value"),
    ):
        assert re.search(rf"# {kind} \w+ {original}$", debug.source, re.MULTILINE)
    assert "local_variable" not in debug.source
    assert "# method " not in debug.source
    assert "# var " not in debug.source
    tree = ast.parse(debug.source)
    owner = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    constructor = next(node for node in owner.body if isinstance(node, ast.FunctionDef))
    attribute_line = next(
        index for index, line in enumerate(debug.source.splitlines(), 1) if "# attr " in line
    )
    assert owner.lineno < attribute_line < constructor.lineno
    for result in (normal, debug):
        namespace: dict[str, Any] = {}
        exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
        assert namespace["__result__"] == 14


def test_debug_tracks_dataclass_fields() -> None:
    originals = {
        Path("main.py"): """
from dataclasses import dataclass
@dataclass
class Record:
    long_field: int
record = Record(long_field=7)
__result__ = record.long_field
"""
    }
    normal = obfuscate_modules(originals, [], jobs=1)
    debug = obfuscate_modules(originals, [], jobs=1, debug=True)
    assert _without_comments(debug.source) == normal.source
    assert re.search(r"# attr \w+ long_field$", debug.source, re.MULTILINE)
    namespace: dict[str, Any] = {}
    exec(debug.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_debug_ignores_definitions_removed_by_minification() -> None:
    originals = {
        Path("main.py"): """
if False:
    class Removed:
        def unused(self): pass
if True:
    def calculate(value): return value + 1
else:
    def unused(value): return value - 1
__result__ = calculate(value=4)
"""
    }
    normal = obfuscate_modules(originals, [], jobs=1)
    debug = obfuscate_modules(originals, [], jobs=1, debug=True)
    assert _without_comments(debug.source) == normal.source
    assert "Removed" not in debug.source
    assert "unused" not in debug.source
    namespace: dict[str, Any] = {}
    exec(debug.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 5


def test_debug_handles_compact_classes_decorators_and_nested_async_functions() -> None:
    source = (
        "class A:x=1\n"
        "@decorate\n"
        "def outer(a,/,*b,c=1,**d):\n"
        "\tasync def inner(e):return e\n"
        "\treturn inner\n"
    )
    tree = ast.parse(source)
    owner = tree.body[0]
    assert isinstance(owner, ast.ClassDef)
    record_attribute(owner, "original_field", "x")
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.__dict__["original_name"] = f"original_{node.name}"
        elif isinstance(node, ast.arg):
            node.__dict__["original_name"] = f"original_{node.arg}"
    debug = annotate_debug(source, tree, "app.min.py")
    assert _without_comments(debug) == source
    assert "class A:x=1\n\t# attr x original_field\n" in debug
    assert "# func outer original_outer\n" in debug
    for name in "abcde":
        assert f"# arg {name} original_{name}\n" in debug
    assert debug.index("# func outer") < debug.index("@decorate")
    assert "\t# func inner original_inner\n" in debug


@pytest.mark.parametrize("flag", ["-d", "--debug"])
def test_cli_debug_flag(tmp_path: Path, flag: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("__result__ = 42\n", encoding="utf-8")
    output = tmp_path / "output"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "obfuscator",
            str(source),
            str(output),
            "--jobs",
            "1",
            "--allowlist",
            "[]",
            flag,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = (output / "app.min.py").read_text(encoding="utf-8")
    assert result.startswith("# app.min.py ")
    assert "# allowlist used" not in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 42

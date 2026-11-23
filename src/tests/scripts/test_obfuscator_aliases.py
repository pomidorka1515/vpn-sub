from __future__ import annotations

import ast
import types
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import pytest

from obfuscator.aliases import consolidate_aliases, shorten_imports
from obfuscator.pipeline import obfuscate_modules

__all__: list[str] = []


def test_all_packed_imports_are_shortened_despite_preserved_names() -> None:
    result = obfuscate_modules(
        {
            Path("main.py"): """
import json
import time
from datetime import datetime
from dataclasses import dataclass
def keep(json, time, datetime):
    return json, time, datetime
@dataclass
class Record:
    time: int
    created: datetime
__result__ = json.loads('3'), time.time() > 0, keep(1, 2, 3)
""",
        },
        [],
    )
    for node in ast.walk(ast.parse(result.source)):
        if (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and getattr(node, "module", "") != "__future__"
        ):
            assert all(alias.asname and alias.asname != alias.name for alias in node.names)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (3, True, (1, 2, 3))
    assert get_type_hints(namespace["Record"], globalns=namespace)["created"].__name__ == "datetime"


def test_aliases_are_hoisted_once_across_packed_modules() -> None:
    result = obfuscate_modules(
        {
            Path("worker.py"): """
First = object
Mapping = dict
Empty = None
""",
            Path("main.py"): """
from worker import First, Mapping, Empty
Second = object
OtherMapping = dict
OtherEmpty = None
__result__ = First is Second, Mapping is OtherMapping, Empty is OtherEmpty
""",
        },
        [],
    )
    tree = ast.parse(result.source)
    for name in ("object", "dict"):
        assert (
            sum(
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id == name
                for node in tree.body
            )
            == 1
        )
    assert (
        sum(
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
            for node in tree.body
        )
        == 1
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (True, True, True)


@pytest.mark.parametrize("value", ["object()", "{}", "[]", "dict()"])
def test_fresh_values_are_not_shared(value: str) -> None:
    tree = ast.parse(f"first = {value}\nsecond = {value}\n__result__ = first is second")
    consolidate_aliases(tree)
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] is False


def test_rebound_shadowed_and_conditional_aliases_are_not_hoisted() -> None:
    tree = ast.parse("""
object = lambda: 1
first = object
second = object
first = 3
if True:
    third = dict
    fourth = dict
def local():
    fifth = dict
    sixth = dict
    return fifth, sixth
""")
    original = ast.unparse(tree)
    consolidate_aliases(tree)
    assert ast.unparse(tree) == original


def test_stdlib_aliases_are_shared_after_imports() -> None:
    tree = ast.parse("""
from pathlib import Path
first = Path
second = Path
__result__ = first is second
""")
    consolidate_aliases(tree)
    assert isinstance(tree.body[0], ast.ImportFrom)
    assert len(tree.body) == 3
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] is True


def test_third_party_aliases_are_not_shared() -> None:
    tree = ast.parse("from quart import request\nfirst = request\nsecond = request")
    original = ast.unparse(tree)
    consolidate_aliases(tree)
    assert ast.unparse(tree) == original


@pytest.mark.parametrize("reflection", ["globals()", "locals()", "vars()", "eval('first')"])
def test_reflective_names_are_preserved(reflection: str) -> None:
    tree = ast.parse(f"import json\nfirst = dict\nsecond = dict\n{reflection}")
    original = ast.unparse(tree)
    consolidate_aliases(tree)
    shorten_imports(tree)
    assert ast.unparse(tree) == original


def test_import_aliases_cannot_be_captured_by_unused_exception_bindings() -> None:
    tree = ast.parse("""
import json
try:
    raise ValueError()
except ValueError as A:
    __result__ = json.loads('3')
""")
    shorten_imports(tree)
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 3


def test_fresh_alias_does_not_capture_an_unresolved_quoted_annotation() -> None:
    tree = ast.parse("import json\nclass Record:\n    missing: 'A'\n")
    shorten_imports(tree)
    statement = tree.body[0]
    assert isinstance(statement, ast.Import)
    assert statement.names[0].asname != "A"


def test_quoted_annotations_follow_aliases_without_changing_shadowed_locals() -> None:
    tree = ast.parse("""
from __future__ import annotations
from datetime import datetime
First = dict
Second = dict
class Record:
    created: 'datetime'
    values: 'First[str, Second]'
def local(datetime):
    return datetime
__result__ = local(3)
""")
    consolidate_aliases(tree)
    shorten_imports(tree)
    module = types.ModuleType("alias_fixture")
    exec(compile(tree, "fixture.py", "exec"), module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixture.
    hints = get_type_hints(module.Record, globalns=module.__dict__)
    assert hints["created"].__name__ == "datetime"
    assert get_origin(hints["values"]) is dict
    assert get_args(hints["values"]) == (str, dict)
    assert module.__dict__["__result__"] == 3

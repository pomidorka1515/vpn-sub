from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from obfuscator.constants import inline_constants
from obfuscator.pipeline import obfuscate_modules

__all__: list[str] = []


def transform(source: str) -> tuple[ast.Module, dict[str, Any]]:
    tree = ast.parse(source)
    inline_constants(tree)
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    return tree, namespace


def test_literals_and_constant_expressions_are_inlined_and_deleted() -> None:
    tree, namespace = transform("""
PREFIX = 'hello'
COUNT: int = 2 + 3
SCALE = -1.5
TEXT = PREFIX + ' world'
LIMIT = COUNT * 4
MESSAGE = f'{TEXT}: {LIMIT!r}'
def result():
    return MESSAGE, SCALE / 2
__result__ = result()
""")
    assert namespace["__result__"] == ("hello world: 20", -0.75)
    assert all(
        name not in namespace for name in ("PREFIX", "COUNT", "SCALE", "TEXT", "LIMIT", "MESSAGE")
    )
    assert not any(isinstance(node, (ast.Assign, ast.AnnAssign)) for node in tree.body[:-1])


@pytest.mark.parametrize("length", [0, 149, 150, 151])
def test_string_length_limit_is_strict(length: int) -> None:
    tree, namespace = transform(f"VALUE = {'x' * length!r}\n__result__ = VALUE\n")
    assert namespace["__result__"] == "x" * length
    assert ("VALUE" in namespace) == (length >= 150)
    assert len(tree.body) == (2 if length >= 150 else 1)


@pytest.mark.parametrize("digits", [149, 150])
def test_integer_length_limit(digits: int) -> None:
    _, namespace = transform(f"VALUE = {'9' * digits}\n__result__ = VALUE\n")
    assert ("VALUE" in namespace) == (digits >= 150)


def test_only_single_assignment_module_bindings_are_removed() -> None:
    tree, namespace = transform("""
VALUE = 3
REBOUND = 1
REBOUND = 2
if True:
    CONDITIONAL = 4
class Record:
    VALUE = 5
def local(VALUE):
    local_value = 6
    return VALUE, local_value, Record.VALUE
def closure():
    VALUE = 7
    return lambda: VALUE
__result__ = VALUE, REBOUND, CONDITIONAL, local(8), closure()()
""")
    assert namespace["__result__"] == (3, 2, 4, (8, 6, 5), 7)
    assert "VALUE" not in namespace
    assert "local_value = 6" in ast.unparse(tree)
    assert "VALUE = 5" in ast.unparse(tree)
    assert "VALUE = 7" in ast.unparse(tree)
    assert namespace["REBOUND"] == 2


@pytest.mark.parametrize("reflection", ["globals()", "locals()", "vars()", "eval('VALUE')"])
def test_reflective_names_are_preserved(reflection: str) -> None:
    source = f"VALUE = 3\n__result__ = {reflection}"
    tree = ast.parse(source)
    original = ast.unparse(tree)
    inline_constants(tree)
    assert ast.unparse(tree) == original


def test_runtime_calls_mutable_values_and_non_literal_types_are_not_evaluated() -> None:
    _, namespace = transform("""
def make():
    return 'value'
CALL = make()
VALUES = []
FLAG = True
EMPTY = None
COMPLEX = 1j
__result__ = CALL, VALUES, FLAG, EMPTY, COMPLEX
""")
    assert all(name in namespace for name in ("CALL", "VALUES", "FLAG", "EMPTY", "COMPLEX"))
    assert namespace["__result__"] == ("value", [], True, None, 1j)


def test_quoted_annotation_dependencies_and_dunders_are_preserved() -> None:
    _, namespace = transform("""
VALUE = 'missing'
NESTED = 'other_missing'
__version__ = '1.0'
class Record:
    field: 'VALUE'
    nested: "list['NESTED']"
""")
    assert namespace["VALUE"] == "missing"
    assert namespace["NESTED"] == "other_missing"
    assert namespace["__version__"] == "1.0"


def test_forward_reads_are_not_changed_into_successful_reads() -> None:
    tree = ast.parse("__result__ = VALUE\nVALUE = 3")
    original = ast.unparse(tree)
    inline_constants(tree)
    assert ast.unparse(tree) == original


def test_global_reassignment_is_not_inlined() -> None:
    _, namespace = transform("""
VALUE = 3
def update():
    global VALUE
    VALUE = 4
update()
__result__ = VALUE
""")
    assert namespace["VALUE"] == 4
    assert namespace["__result__"] == 4


def test_class_reexports_do_not_count_as_module_reassignments() -> None:
    tree, namespace = transform("""
VALUE = 3
class Record:
    VALUE = VALUE
__result__ = VALUE, Record.VALUE
""")
    assert "VALUE" not in namespace
    assert namespace["__result__"] == (3, 3)
    assert "VALUE = 3" in ast.unparse(tree)


def test_functions_defined_before_a_constant_retain_late_lookup() -> None:
    tree, namespace = transform("""
def read():
    return VALUE
VALUE = 3
__result__ = read()
""")
    assert namespace["__result__"] == 3
    assert "VALUE = 3" in ast.unparse(tree)


def test_large_computed_values_and_nonfinite_floats_stay_runtime() -> None:
    _, namespace = transform("""
LARGE = 'x' * 150
INFINITE = 1e309
NEGATIVE_ZERO = -0.0
__result__ = LARGE, INFINITE, NEGATIVE_ZERO
""")
    assert namespace["LARGE"] == "x" * 150
    assert "INFINITE" in namespace
    assert "NEGATIVE_ZERO" not in namespace
    assert repr(namespace["__result__"][2]) == "-0.0"


def test_definition_order_uses_packed_order_not_original_line_numbers() -> None:
    result = obfuscate_modules(
        {
            Path("worker.py"): "\n" * 30 + "VALUE = 3\n",
            Path("main.py"): "from worker import VALUE\n__result__ = VALUE + 2\n",
        },
        [],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 5
    assert "VALUE" not in result.source


def test_packing_collisions_and_local_shadowing_keep_distinct_values() -> None:
    result = obfuscate_modules(
        {
            Path("first.py"): "VALUE = 2\ndef read(): return VALUE\n",
            Path("second.py"): "VALUE = 'hello'\ndef read(): return VALUE\n",
            Path("main.py"): """
from first import read as first
from second import read as second
def local(VALUE):
    return VALUE
__result__ = first(), second(), local(4)
""",
        },
        [],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (2, "hello", 4)
    assert not any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) in {str, int, float}
        for node in ast.parse(result.source).body
    )


@pytest.mark.parametrize(
    "expression", ["'x' * 10**9", "10 ** 1000000", "1 << 1000000", "'%1000000000s' % 'x'", "1 / 0"]
)
def test_expensive_or_failing_expressions_are_not_evaluated(expression: str) -> None:
    tree = ast.parse(f"VALUE = {expression}\n__result__ = VALUE")
    original = ast.unparse(tree)
    inline_constants(tree)
    assert ast.unparse(tree) == original


def test_exception_timing_is_unchanged() -> None:
    tree = ast.parse("VALUE = 1 / 0\n__result__ = VALUE")
    inline_constants(tree)
    with pytest.raises(ZeroDivisionError):
        exec(compile(tree, "fixture.py", "exec"), {})  # ruff: ignore[exec-builtin] - Fixed fixture.

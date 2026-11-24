from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from obfuscator.pipeline import obfuscate, obfuscate_modules

__all__: list[str] = []


@pytest.mark.parametrize("decorator", ["", "@dataclass(frozen=True, slots=True)"])
def test_constructor_only_class_names_are_shortened(decorator: str) -> None:
    source = f"""
from dataclasses import dataclass
{decorator}
class InternalRecord:
    value: int = 7
def construct() -> InternalRecord:
    return InternalRecord()
__result__ = construct().value
"""
    for output in (
        obfuscate(source, "fixture.py"),
        obfuscate_modules({Path("main.py"): source}, [], jobs=1).source,
    ):
        assert "InternalRecord" not in output
        namespace: dict[str, Any] = {"__name__": __name__}
        exec(output, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
        assert namespace["__result__"] == 7


@pytest.mark.parametrize(
    "use",
    [
        "alias = PublicRecord",
        "registry = [PublicRecord]",
        "name = PublicRecord.__name__",
        "class Child(PublicRecord): pass",
        "@dataclass\nclass Schema:\n    value: PublicRecord",
        "@dataclass\nclass Schema:\n    value: 'PublicRecord'",
        "PublicRecord = object",
        "namespace = globals()",
        "name = record.__class__.__name__",
        "name = type(record).__name__",
        "def inspect(record: PublicRecord):\n    return record.__class__.__name__",
    ],
)
def test_nonconstructor_class_uses_preserve_names(use: str) -> None:
    output = obfuscate(
        f"from dataclasses import dataclass\nclass PublicRecord: pass\n"
        f"record = PublicRecord()\n{use}\n",
        "fixture.py",
    )
    assert any(
        isinstance(node, ast.ClassDef) and node.name == "PublicRecord"
        for node in ast.walk(ast.parse(output))
    )


def test_cross_module_constructor_and_shadowed_names() -> None:
    output = obfuscate_modules(
        {
            Path("records.py"): "class InternalRecord:\n    value = 9\n",
            Path("main.py"): """
from records import InternalRecord
def unrelated(InternalRecord):
    return InternalRecord + 1
__result__ = InternalRecord().value, unrelated(2)
""",
        },
        [],
        jobs=1,
    ).source
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "InternalRecord"
        for node in ast.walk(ast.parse(output))
    )
    namespace: dict[str, Any] = {"__name__": __name__}
    exec(output, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (9, 3)


def test_name_mangled_members_and_parameters_preserve_class_names() -> None:
    output = obfuscate(
        """
class InternalRecord:
    def read(self, __value):
        self.__value = __value
        return self.__value
__result__ = InternalRecord().read(_InternalRecord__value=7)
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(output, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7
    assert "class InternalRecord:" in output


def test_quoted_schema_dependency_does_not_block_unrelated_classes() -> None:
    output = obfuscate(
        """
from dataclasses import dataclass
class SchemaValue: pass
@dataclass
class Schema:
    value: 'SchemaValue'
class InternalRecord:
    value = 7
schema_value = SchemaValue()
__result__ = InternalRecord().value
""",
        "fixture.py",
    )
    names = {node.name for node in ast.walk(ast.parse(output)) if isinstance(node, ast.ClassDef)}
    assert "SchemaValue" in names
    assert "InternalRecord" not in names


def test_name_mangled_slots_preserve_class_names() -> None:
    output = obfuscate(
        """
class InternalRecord:
    __slots__ = ('__value',)
record = InternalRecord()
record._InternalRecord__value = 7
__result__ = record._InternalRecord__value
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(output, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7

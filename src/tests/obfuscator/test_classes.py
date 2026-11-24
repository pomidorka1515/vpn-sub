from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import obfuscator
from obfuscator.pipeline import minify_tree, obfuscate, obfuscate_modules
from tests.support.obfuscator import flattened_modules

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


@pytest.mark.parametrize("linked", [False, True])
def test_private_classes_use_g_names_and_public_classes_stay(linked: bool) -> None:
    source = """
class _Private: pass
class Public: pass
def create():
    class _Local: pass
    return _Local
__result__ = _Private.__name__, Public.__name__, create().__name__
"""
    if linked:
        source = flattened_modules({"worker": source, "main": "from worker import __result__\n"})
        result = minify_tree(ast.parse(source), "fixture.py", rename_globals=True)
    else:
        result = obfuscator.obfuscate(source, "fixture.py")
    classes = [node.name for node in ast.walk(ast.parse(result)) if isinstance(node, ast.ClassDef)]
    assert "Public" in classes
    assert len(classes) == 3
    assert all(name == "Public" or name.startswith("_g") for name in classes)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    private, public, local = namespace["__result__"]
    assert private.startswith("_g")
    assert local.startswith("_g")
    assert private != local
    assert public == "Public"


def test_private_class_forward_annotations_follow_renamed_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = flattened_modules(
        {
            "models": "from dataclasses import dataclass\n"
            "@dataclass\nclass _Item:\n value: int\n"
            "@dataclass\nclass Record:\n item: '_Item'\n",
            "main": "from models import Record, _Item\nfrom dacite import from_dict\n"
            "record = from_dict(Record, {'item': {'value': 3}})\n"
            "__result__ = record.item.value, isinstance(record.item, _Item), _Item.__name__\n",
        }
    )
    result = minify_tree(ast.parse(result), "fixture.py", rename_globals=True)
    module = types.ModuleType("private_class_annotations_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(result, module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert module.__result__[:2] == (3, True)
    assert module.__result__[2].startswith("_g")


def test_private_member_classes_keep_attribute_access() -> None:
    result = obfuscator.obfuscate(
        "class Public:\n"
        " if True:\n  class _Private:\n   value = 7\n"
        " Alias = _Private\n"
        "class Child(Public): pass\n"
        "result = (Public._Private.value, getattr(Child, '_Private').__name__, "
        "Public.Alias is Public._Private, hasattr(Public, Public._Private.__name__))\n",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    value, name, same, leaked_alias = namespace["result"]
    assert value == 7
    assert name.startswith("_g")
    assert same
    assert not leaked_alias

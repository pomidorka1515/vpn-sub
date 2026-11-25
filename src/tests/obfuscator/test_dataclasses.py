from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from obfuscator.dataclasses import minify_dataclasses
from obfuscator.pipeline import obfuscate, obfuscate_modules, prepare_tree

__all__: list[str] = []


def transform(source: str) -> tuple[ast.Module, dict[str, Any]]:
    tree = prepare_tree(source, "fixture.py")
    minify_dataclasses(tree)
    module = ModuleType("fixture")
    namespace: dict[str, Any] = module.__dict__
    with patch.dict(sys.modules, {"fixture": module}):
        exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    return tree, namespace


def fields(tree: ast.Module, name: str) -> dict[str, str]:
    owner = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == name
    )
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in owner.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def test_strips_and_renames_deep_fields_and_constructor_keywords() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass, field
@dataclass(slots=True, frozen=True)
class Leaf:
    value: int
@dataclass
class Branch:
    leaf: Leaf
@dataclass
class Root:
    branches: list[Branch]
    def read(self):
        return self.branches[0].leaf.value
class Holder:
    def __init__(self, res: Root):
        self.res = res
    def read(self):
        return self.res.branches[0].leaf.value
leaf = Leaf(**{'value': 7})
root = Root(branches=[Branch(leaf)])
holder = Holder(root)
__result__ = root.read(), root.branches[0].leaf.value, holder.read()
""")
    assert namespace["__result__"] == (7, 7, 7)
    for owner, original in (("Leaf", "value"), ("Branch", "leaf"), ("Root", "branches")):
        assert original not in fields(tree, owner)
        assert set(fields(tree, owner).values()) == {"0"}


def test_asdict_preserves_nested_names_but_not_annotations() -> None:
    tree, namespace = transform("""
import dataclasses as dc
serialize = dc.asdict
@dc.dataclass
class Leaf:
    value: int
@dc.dataclass
class Root:
    leaves: list[Leaf]
__result__ = serialize(Root([Leaf(4)]))
""")
    assert namespace["__result__"] == {"leaves": [{"value": 4}]}
    assert fields(tree, "Root") == {"leaves": "0"}
    assert fields(tree, "Leaf") == {"value": "0"}


def test_dacite_protects_transitive_container_types_only() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass
from dacite import from_dict as decode, Config
@dataclass
class Leaf:
    value: int
@dataclass
class Root:
    leaves: list[Leaf]
@dataclass
class Independent:
    count: int
config = Config()
root = decode(data_class=Root, data={'leaves': [{'value': 5}]}, config=config)
__result__ = root.leaves[0].value, Independent(count=9).count
""")
    assert namespace["__result__"] == (5, 9)
    assert fields(tree, "Leaf") == {"value": "int"}
    assert fields(tree, "Root") == {"leaves": "list[Leaf]"}
    assert "count" not in fields(tree, "Independent")
    assert set(fields(tree, "Independent").values()) == {"0"}


def test_dynamic_dacite_registry_protects_its_classes_not_unrelated_records() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass
from dacite import from_dict
@dataclass
class Schema:
    value: int
@dataclass
class Independent:
    count: int
schemas = [Schema]
plain = Schema(value=1)
decoded = from_dict(schemas[0], {'value': 4})
__result__ = decoded.value, Independent(count=6).count
""")
    assert namespace["__result__"] == (4, 6)
    assert fields(tree, "Schema") == {"value": "int"}
    assert "count" not in fields(tree, "Independent")


@pytest.mark.parametrize(
    "use",
    [
        "getattr(record, 'value')",
        "record.__dict__",
        "fields(record)",
        "get_type_hints(Record)",
    ],
)
def test_reflection_preserves_fields_and_types(use: str) -> None:
    tree, _ = transform(f"""
from dataclasses import dataclass, fields
from typing import get_type_hints
@dataclass
class Record:
    value: int
record = Record(value=3)
__result__ = {use}
""")
    assert fields(tree, "Record") == {"value": "int"}


def test_dynamic_kwargs_preserve_names_but_can_strip_types() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass
@dataclass
class Record:
    value: int
kwargs = dict(value=5)
__result__ = Record(**kwargs).value
""")
    assert namespace["__result__"] == 5
    assert fields(tree, "Record") == {"value": "0"}


@pytest.mark.parametrize("directive", ["ClassVar[int] = 1", "InitVar[int] = 1", "KW_ONLY"])
def test_dataclass_directives_are_not_replaced(directive: str) -> None:
    tree, _ = transform(f"""
from dataclasses import dataclass, InitVar, KW_ONLY
from typing import ClassVar
@dataclass
class Record:
    marker: {directive}
    value: int = 2
__result__ = Record().value
""")
    assert fields(tree, "Record")["value"] == "int"
    assert fields(tree, "Record")["marker"] != "0"


def test_unknown_receivers_do_not_get_rewritten_by_name() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass
@dataclass
class Record:
    value: int
def read(obj):
    return obj.value
__result__ = read(Record(3))
""")
    assert namespace["__result__"] == 3
    assert "value" in fields(tree, "Record")


@pytest.mark.parametrize(
    "body",
    [
        "record = Record(3)\nrecord = None\n",
        "def factory():\n    return Record(3)\n",
        "records = [Record(3)]\nrecords = []\n",
        "records = (Record(3),)\nrecords = ()\n",
        "records = {'record': Record(3)}\nrecords = {}\n",
        "records = [[Record(3)]]\nrecords = []\n",
        "records = [Record(3)]\nalias = records\nrecords = []\n",
        "class Child(Record):\n    pass\nrecord = Record(3)\n",
        "record = Record(3)\nmatch record:\n    case Record(value=result):\n        pass\n",
    ],
)
def test_untracked_flows_and_hierarchies_are_preserved(body: str) -> None:
    tree, _ = transform(
        """
from dataclasses import dataclass
@dataclass
class Record:
    value: int
"""
        + body
    )
    assert fields(tree, "Record") == {"value": "int"}


def test_annotated_internal_factory_and_consumer_are_rewritten() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass
@dataclass
class Record:
    value: int
def factory() -> Record:
    return Record(value=6)
def read(record: Record) -> int:
    return record.value
__result__ = read(factory())
""")
    assert namespace["__result__"] == 6
    assert "value" not in fields(tree, "Record")


def test_escaping_annotated_callback_protects_records() -> None:
    tree, _ = transform("""
from dataclasses import dataclass
@dataclass
class Record:
    value: int
def factory() -> Record:
    return Record(value=6)
callbacks = [factory]
""")
    assert fields(tree, "Record") == {"value": "int"}


def test_explicit_slots_are_not_renamed() -> None:
    tree, namespace = transform("""
from dataclasses import dataclass
@dataclass
class Record:
    __slots__ = ('value',)
    value: int
__result__ = Record(value=5).value
""")
    assert namespace["__result__"] == 5
    assert fields(tree, "Record") == {"value": "int"}


def test_cross_module_pipeline_and_standalone_minification() -> None:
    sources = {
        Path("records.py"): """
from dataclasses import dataclass
@dataclass
class Record:
    value: int
""",
        Path("main.py"): """
from records import Record
__result__ = Record(value=42).value
""",
    }
    output = obfuscate_modules(sources, [], jobs=1).source
    namespace: dict[str, Any] = {"__name__": __name__}
    exec(output, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 42
    assert "value" not in output
    standalone = obfuscate(
        sources[Path("records.py")] + "\n__result__ = Record(value=42).value", "fixture.py"
    )
    exec(standalone, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 42
    assert "value" not in standalone

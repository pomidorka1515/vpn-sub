from __future__ import annotations

import ast
import sys
import types
from typing import Any

import pytest

pytest.importorskip("python_minifier")

import obfuscator

__all__: list[str] = []


@pytest.mark.parametrize(
    "imports",
    [
        "from typing import overload",
        "from typing import overload as overloaded",
        "import typing",
        "import typing as t",
    ],
)
def test_overloads_and_unused_typing_imports_are_removed(imports: str) -> None:
    decorator = {
        "from typing import overload": "overload",
        "from typing import overload as overloaded": "overloaded",
        "import typing": "typing.overload",
        "import typing as t": "t.overload",
    }[imports]
    result = obfuscator.obfuscate(
        f"""
{imports}
@{decorator}
def choose(value: int) -> int: ...
def choose(value):
    return value
result = choose(3)
""",
        "fixture.py",
    )
    assert "overload" not in result
    assert "typing" not in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 3


def test_runtime_field_annotations_are_preserved() -> None:
    result = obfuscator.obfuscate(
        """
from __future__ import annotations
from dataclasses import dataclass, field
from typing import NamedTuple
@dataclass
class Record:
    required: MissingType
    values: list[int] = field(default_factory=list[int])
class Pair(NamedTuple):
    left: MissingType
    right: int
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    record = namespace["Record"](required="value")
    assert record.values == []
    assert namespace["Pair"]("left", 2) == ("left", 2)
    assert namespace["Record"].__annotations__ == {"required": "MissingType", "values": "list[int]"}
    pair = next(
        node
        for node in ast.parse(result).body
        if isinstance(node, ast.ClassDef) and node.name == "Pair"
    )
    assert [
        ast.unparse(node.annotation) for node in pair.body if isinstance(node, ast.AnnAssign)
    ] == [
        "MissingType",
        "int",
    ]


@pytest.mark.parametrize(
    ("imports", "literal"),
    [
        ("from typing import Literal", "Literal"),
        ("from typing import Literal as L", "L"),
        ("import typing", "typing.Literal"),
        ("import typing as t", "t.Literal"),
        ("from typing_extensions import Literal as L", "L"),
    ],
)
@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        ('{literal}["secret", "other"]', "str"),
        ("{literal}[True, 0]", "bool | int"),
        ("{literal}[0, True, 1, False]", "int | bool"),
        ('{literal}["secret", True, 0, None]', "str | bool | int | None"),
        ("{literal}[-1, +2]", "int"),
        ("{literal}[b'secret', b'other']", "bytes"),
        ("{literal}[None]", "None"),
        ('{literal}["secret", {literal}[True, "other"], False]', "str | bool"),
        ('dict[str, list[{literal}["secret", "other"]]]', "dict[str, list[str]]"),
    ],
)
def test_runtime_literals_are_collapsed_before_cleanup(
    imports: str, literal: str, annotation: str, expected: str
) -> None:
    source: str = f"""
from __future__ import annotations
from dataclasses import dataclass
{imports}
@dataclass
class Record:
    value: {annotation.format(literal=literal)}
"""
    result: str = obfuscator.obfuscate(source, "fixture.py")
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["Record"].__annotations__ == {"value": expected}
    assert "secret" not in result
    assert "typing" not in result


def test_enum_literals_are_collapsed_to_the_enum_type() -> None:
    source: str = """
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Literal
class Choice(Enum):
    FIRST = 1
    SECOND = 2
@dataclass
class Record:
    value: Literal[Choice.FIRST, Choice.SECOND]
"""
    result: str = obfuscator.obfuscate(source, "fixture.py")
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["Record"].__annotations__ == {"value": "Choice"}
    assert "Literal" not in result


def test_dacite_constructs_nested_dataclasses_after_obfuscation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
@dataclass
class Client:
    traffic: int
@dataclass
class Response:
    obj: list[Client]
    status: Literal["ok"]
"""
    module = types.ModuleType("obfuscated_dacite_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(obfuscator.obfuscate(source, "fixture.py"), module.__dict__)  # ruff: ignore[exec-builtin]
    from dacite import from_dict

    response = from_dict(module.Response, {"obj": [{"traffic": 12}], "status": "other"})
    assert isinstance(response.obj[0], module.Client)
    assert response.obj[0].traffic == 12
    assert response.status == "other"
    assert module.Response.__annotations__["status"] == "str"


@pytest.mark.parametrize(
    ("imports", "decorator", "base"),
    [
        (
            "from dataclasses import dataclass\nfrom typing import NamedTuple",
            "dataclass",
            "NamedTuple",
        ),
        (
            "from dataclasses import dataclass as dc\nfrom typing import NamedTuple as NT",
            "dc",
            "NT",
        ),
        ("import dataclasses as dc\nimport typing as t", "dc.dataclass", "t.NamedTuple"),
    ],
)
def test_only_runtime_class_fields_keep_annotations(
    imports: str, decorator: str, base: str
) -> None:
    source = f"""
from __future__ import annotations
{imports}
module_value: int = 2
module_only: int
@{decorator}(slots=True)
class Record:
    value: int
    def double(self, scale: int = 2) -> int:
        local: int = self.value * scale
        return local
    class Ordinary:
        unused: int
        value: int = 3
class Pair({base}):
    left: int
    right: int = 4
result = Record(value=3).double(), Pair(left=2), Record.Ordinary.value, module_value
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    tree = ast.parse(result)
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            assert node.annotation is None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.returns is None
    fields = [
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert fields == ["value", "left", "right"]
    original: dict[str, Any] = {}
    release: dict[str, Any] = {}
    exec(source, original)  # ruff: ignore[exec-builtin] - Fixed fixture.
    exec(result, release)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert original["result"] == release["result"]


def test_annotation_only_locals_keep_unbound_local_behavior() -> None:
    source = """
value = 3
def run():
    value: int
    try:
        return value
    except UnboundLocalError:
        return 'unbound'
result = run()
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    assert not any(isinstance(node, ast.AnnAssign) for node in ast.walk(ast.parse(result)))
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == "unbound"


def test_dataclass_field_markers_keep_constructor_behavior() -> None:
    source = """
from dataclasses import dataclass, InitVar, KW_ONLY, fields
from typing import ClassVar
@dataclass
class Record:
    shared: ClassVar[int] = 3
    value: int
    _: KW_ONLY
    initial: InitVar[int] = 4
    def __post_init__(self, initial):
        self.value += initial
record = Record(value=2, initial=5)
result = record.value, tuple(field.name for field in fields(Record))
"""
    namespace: dict[str, Any] = {}
    code = compile(
        obfuscator.obfuscate(source, "fixture.py"), "fixture.py", "exec", dont_inherit=True
    )
    exec(code, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (7, ("value",))


def test_annotation_only_targets_preserve_evaluation_order() -> None:
    source = """
events = []
def receiver():
    events.append('receiver')
    return None
def index(value):
    events.append(value)
    return value
receiver().missing: int
receiver()[index(1)]: int
receiver()[index(2):index(3):index(4)]: int
receiver()[index(5):index(6), index(7)]: int
"""
    original: dict[str, Any] = {}
    release: dict[str, Any] = {}
    exec(source, original)  # ruff: ignore[exec-builtin] - Fixed fixture.
    exec(obfuscator.obfuscate(source, "fixture.py"), release)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert original["events"] == release["events"]


def test_removed_generic_parameters_are_replaced_with_object() -> None:
    result = obfuscator.obfuscate(
        """
class Box[T](list[T]):
    pass
result = Box[int]([1, 2])
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == [1, 2]


@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
@pytest.mark.parametrize("style", ["direct", "aliased", "qualified"])
def test_typed_dict_schemas_are_replaced_with_dict(module: str, style: str) -> None:
    imports, base = {
        "direct": (f"from {module} import TypedDict", "TypedDict"),
        "aliased": (f"from {module} import TypedDict as TD", "TD"),
        "qualified": (f"import {module} as t", "t.TypedDict"),
    }[style]
    result = obfuscator.obfuscate(
        f"""
{imports}
class Payload({base}, total=False):
    secret_schema_field: MissingType
class Extended(Payload):
    other_schema_field: OtherMissingType
class Generic[T]({base}):
    generic_schema_field: T
class Specialized(Generic[int]):
    specialized_schema_field: int
Functional = {base}("Functional", {{"functional_schema_field": MissingType}}, total=False)
payload = Payload(value=1)
extended = Extended(value=2)
functional = Functional(value=3)
""",
        "fixture.py",
    )
    assert "schema_field" not in result
    assert "MissingType" not in result
    assert "TypedDict" not in result
    assert not any(isinstance(node, ast.ClassDef) for node in ast.walk(ast.parse(result)))
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    for name in ("Payload", "Extended", "Generic", "Specialized", "Functional"):
        assert namespace[name] is dict
    assert namespace["payload"] == {"value": 1}
    assert namespace["extended"] == {"value": 2}
    assert namespace["functional"] == {"value": 3}


@pytest.mark.parametrize(
    ("imports", "field"),
    [
        ("from dataclasses import field", "field"),
        ("from dataclasses import field as make_field", "make_field"),
        ("import dataclasses", "dataclasses.field"),
        ("import dataclasses as dc", "dc.field"),
    ],
)
def test_dataclass_default_factory_generics_are_removed(imports: str, field: str) -> None:
    result = obfuscator.obfuscate(
        f"""
{imports}
class Example:
    totals: dict[str, list[int]] = {field}(default_factory=dict[str, list[int]])
    entries: list[int] = {field}(default_factory=list[int])
    plain = {field}(default_factory=dict)
    callback = {field}(default_factory=lambda: [1])
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    example = namespace["Example"]
    assert example.totals.default_factory is dict
    assert example.entries.default_factory is list
    assert example.plain.default_factory is dict
    assert example.callback.default_factory() == [1]
    assert not any(isinstance(node, ast.Subscript) for node in ast.walk(ast.parse(result)))


def test_runtime_subscripts_outside_dataclass_factories_are_preserved() -> None:
    result = obfuscator.obfuscate(
        """
from dataclasses import field
values = [dict[str, list[int]]]
selected = values[0]
metadata = field(default=values[0])
def other(**kwargs): return kwargs
unrelated = other(default_factory=values[0])
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["selected"] == dict[str, list[int]]
    assert namespace["metadata"].default == dict[str, list[int]]
    assert namespace["unrelated"]["default_factory"] == dict[str, list[int]]


def test_empty_suites_and_assigned_strings() -> None:
    result = obfuscator.obfuscate(
        """
message = "keep this"
class Empty:
    "documentation"
def empty():
    value: int
if message:
    "unused string"
try:
    "unused string"
except Exception:
    "unused string"
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Execute a fixed fixture, not external input.
    assert namespace["message"] == "keep this"
    assert namespace["empty"]() is None


def test_signatures_keep_variadics() -> None:
    result = obfuscator.obfuscate(
        """
def regular(value=1, /, *, required, optional=2):
    return value, required, optional
def variadic(value, /, *items: int, flag: bool = False, **options: str):
    return value, items, flag, options
async def asynchronous(value: int, /, *, flag: bool = False) -> int:
    return value
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Execute a fixed fixture, not external input.
    assert namespace["regular"](required=3) == (1, 3, 2)
    assert namespace["variadic"](1, 2, 3, flag=True, extra="yes") == (
        1,
        (2, 3),
        True,
        {"extra": "yes"},
    )
    assert namespace["asynchronous"].__annotations__ == {}

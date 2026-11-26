from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from obfuscator.annotations import AnnotationIndex, TypeHint, merge_hints
from obfuscator.linker import flatten
from obfuscator.pipeline import obfuscate_modules
from obfuscator.renamers.allowlist import AllowlistRenamer
from obfuscator.renamers.attribute_flow import AttributeFlow
from obfuscator.renamers.base import ArgumentRenamer
from obfuscator.renamers.external import ExternalReturns
from obfuscator.renamers.stub_returns import StubReturns
from tests.support.obfuscator import allowlisted

__all__: list[str] = []


def test_receiver_diagnostics_preserve_source_lines_after_relocation() -> None:
    tree = flatten({"example": ast.parse("def read(value):\n    return value.row\n")})
    ast.increment_lineno(tree, 1000)
    analysis = AllowlistRenamer(tree)
    flow = AttributeFlow(analysis)
    node = next(node for node in analysis.nodes if isinstance(node, ast.Attribute))
    assert flow.describe(node) == "'value.row' at example.py:2 (read)"


def test_async_context_manager_through_receiver_fields() -> None:
    source = """
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
class Connection:
    async def row(self): return 7
class Database:
    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Connection]:
        yield Connection()
class Resources:
    db: Database
class Subscription:
    res: Resources
class Api:
    sub: Subscription
    async def health(self):
        async with self.sub.res.db.connection() as conn:
            return await conn.row()
"""
    result, names = allowlisted(source, ["f:Connection:row*"])
    assert names["f:Connection:row*"] in result


def test_reexported_receiver_context_manager_survives_full_pipeline() -> None:
    sources = {
        Path("records.py"): "class Connection:\n    async def row(self): return 7\n",
        Path("implementation.py"): """
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from records import Connection
class Database:
    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Connection]:
        yield Connection()
""",
        Path("facade.py"): "from implementation import Database as Exported\n",
        Path("public.py"): "from facade import Exported as PublicDatabase\n",
        Path("api.py"): """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from public import PublicDatabase
class Api:
    def __init__(self, db: PublicDatabase): self.db = db
    async def health(self):
        async with self.db.connection() as conn:
            return await conn.row()
""",
        Path("main.py"): """
import asyncio
from implementation import Database
from api import Api
__result__ = asyncio.run(Api(Database()).health())
""",
    }
    result = obfuscate_modules(sources, ["f:Connection:row*"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_receiver_alias_cycles_retain_uncertainty() -> None:
    tree = ast.parse("")
    tree.__dict__["annotation_aliases"] = {
        "facade.First": TypeHint(("facade.Second",)),
        "facade.Second": TypeHint(("facade.First",)),
    }
    analysis = ArgumentRenamer({"fixture": tree})
    hint = analysis.canonical_hint(TypeHint(("facade.First", "builtins.str")))
    assert hint.classes == ("builtins.str",)
    assert hint.incomplete


@pytest.mark.parametrize("origin", ["tuple", "sequence", "awaitable", "async_contextmanager"])
def test_receiver_aliases_preserve_nested_shapes(origin: str) -> None:
    tree = ast.parse("")
    tree.__dict__["annotation_aliases"] = {
        "public.Record": TypeHint(("facade.Record",)),
        "facade.Record": TypeHint(("implementation.Record",)),
        "public.Container": TypeHint(("implementation.Container",)),
    }
    analysis = ArgumentRenamer({"fixture": tree})
    nested = TypeHint(("public.Container",), arguments=(TypeHint(("public.Record",)),))
    hint = analysis.canonical_hint(TypeHint(origin=origin, arguments=(nested,)))
    assert hint.origin == origin
    assert hint.arguments[0].classes == ("implementation.Container",)
    assert hint.arguments[0].arguments[0].classes == ("implementation.Record",)
    assert not hint.incomplete


def test_optional_self_rebinding_reads_incoming_annotation() -> None:
    tree = ast.parse("""
class Options:
    value: int
def read(options: Options | None):
    options = options or Options()
    return options.value
""")
    AnnotationIndex("fixture", tree).apply()
    analysis = ArgumentRenamer({"fixture": tree})
    value = next(node for node in analysis.nodes if isinstance(node, ast.Attribute))
    assert analysis.value_hint(value).classes == ("builtins.int",)
    assert not analysis.value_hint(value).incomplete


@pytest.mark.parametrize("container", ["[*resources]", "(*resources,)", "[*resources, *([] if flag else resources)]"])
def test_starred_resource_containers_preserve_receiver_flow(container: str) -> None:
    source = f"""
from dataclasses import dataclass
class Selected:
    def close(self): return 7
class Other:
    def close(self): return 3
@dataclass
class Owned:
    resource: object
resources = [Owned(Other())]
flag = True
expanded = {container}
__result__ = Selected().close(), expanded[-1].resource.close()
"""
    result, _ = allowlisted(source, ["f:Selected:close"])
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (7, 3)


def test_annotated_base_table_does_not_add_an_opaque_consumer() -> None:
    source = """
from typing import ClassVar
class Record:
    def __init__(self): self.value = 7
class Route:
    def register(self, api): return api.record.value
class Base:
    ROUTES: ClassVar[tuple[Route, ...]]
    def __init__(self, record): self.record = record
    def run(self):
        return [route.register(self) for route in self.ROUTES]
routes = (Route(),)
class Concrete(Base):
    ROUTES = (*routes,)
__result__ = Concrete(Record()).run()
"""
    result, _ = allowlisted(source, ["attr:Record:value"])
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == [7]


@pytest.mark.parametrize("expression", [
    "record if flag else opaque()", "record or opaque()",
    "[record, opaque()][0]", "next(iter((record, opaque())))",
    "{'known': record, **unknown}['known']",
    "{'known': record}.get('missing', opaque())",
])
def test_expression_alternatives_retain_uncertainty(expression: str) -> None:
    hint = expression_hint("""
class Record: pass
record = Record()
def opaque(): pass
""", expression)
    assert hint.incomplete


def test_tuple_alternatives_do_not_invent_fixed_positions() -> None:
    record = TypeHint(("fixture.Record",))
    other = TypeHint(("fixture.Other",))
    merged = merge_hints(
        TypeHint(origin="tuple", arguments=(record,)),
        TypeHint(origin="tuple", arguments=(other, record)),
    )
    assert merged.variadic
    assert set(merged.element(0).classes) == {"fixture.Record", "fixture.Other"}


@pytest.mark.parametrize("value", [
    "record if flag else opaque()", "record or opaque()",
    "[record, opaque()][0]", "next(iter((record, opaque())))",
    "{'known': record}.get('missing', opaque())",
])
def test_uncertain_alternatives_cannot_rename_a_method(value: str) -> None:
    source = f"""
class Record:
    def close(self): return 7
def run(flag, opaque):
    record = Record()
    value = {value}
    return value.close()
"""
    with pytest.raises(ValueError, match="Unresolved receiver"):
        allowlisted(source, ["f:Record:close*"])


def expression_hint(source: str, expression: str) -> TypeHint:
    tree = ast.parse(f"{source}\n__probe__ = {expression}\n")
    AnnotationIndex("fixture", tree).apply()
    analysis = ArgumentRenamer({"fixture": tree})
    statement = tree.body[-1]
    assert isinstance(statement, ast.Assign)
    return analysis.value_hint(statement.value)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("next(iter(records))", "Record"),
        ("list(reversed(records))[0]", "Record"),
        ("tuple(records)[-1]", "Record"),
        ("sorted(records)[0]", "Record"),
        ("list(enumerate(records))[0][1]", "Record"),
        ("list(zip(records, others))[0][0]", "Record"),
        ("list(zip(records, others))[0][1]", "Other"),
        ("list(mapping.values())[0]", "Record"),
        ("list(mapping.items())[0][1]", "Record"),
        ("dict(mapping).get('key')", "Record"),
        ("dict(zip(others, records)).get('key')", "Record"),
        ("(Record(), Other())[1:][0]", "Other"),
        ("(await asyncio.gather(fetch(), other()))[0]", "Record"),
        ("(await asyncio.gather(fetch(), other()))[1]", "Other"),
        ("(await asyncio.gather(*(fetch() for _ in records)))[0]", "Record"),
        ("(await asyncio.gather(*[fetch(), fetch()]))[0]", "Record"),
        ("await asyncio.wait_for(asyncio.shield(asyncio.create_task(fetch())), 1)", "Record"),
    ],
)
def test_composed_structural_operations(expression: str, expected: str) -> None:
    hint = expression_hint("""
import asyncio
class Record: pass
class Other: pass
records: list[Record]
others: list[Other]
mapping: dict[str, Record]
async def fetch() -> Record: return Record()
async def other() -> Other: return Other()
""", expression)
    assert hint.classes == (f"fixture.{expected}",)


@pytest.mark.parametrize("operation", ["zip", "enumerate", "list", "iter", "next"])
def test_shadowed_builtins_do_not_use_standard_transfer_rules(operation: str) -> None:
    hint = expression_hint(f"""
class Record: pass
class Other: pass
def {operation}(*args) -> Other: return Other()
records: list[Record]
""", f"{operation}(records)")
    assert hint.classes == ("fixture.Other",)


def test_gather_retains_exception_alternative() -> None:
    hint = expression_hint("""
from asyncio import gather as collect
class Record: pass
async def fetch() -> Record: return Record()
""", "(await collect(fetch(), return_exceptions=True))[0]")
    assert set(hint.classes) == {"fixture.Record", "builtins.BaseException"}


@pytest.mark.parametrize("expression", ["(await asyncio.gather(fetch(), opaque()))[1]", "next(iter(await asyncio.gather(fetch(), opaque())))"])
def test_gather_does_not_drop_unresolved_results(expression: str) -> None:
    hint = expression_hint("""
import asyncio
class Record: pass
async def fetch() -> Record: return Record()
async def opaque(): pass
""", expression)
    assert hint.incomplete


def test_async_call_retains_awaitable_layer() -> None:
    hint = expression_hint("""
from collections.abc import Awaitable
class Record: pass
async def fetch() -> Awaitable[Record]: pass
""", "await fetch()")
    assert hint.origin == "awaitable"
    assert hint.element().classes == ("fixture.Record",)


@pytest.mark.parametrize("expression", ["identity(record)", "first([record])", "first(values)", "unwrap(box)", "box.value", "box.get()", "Box(record).value", "Box(record).get()", "await later(record)"])
def test_generic_shapes_are_instantiated(expression: str) -> None:
    hint = expression_hint("""
class Record: pass
class Box[T]:
    value: T
    def __init__(self, value: T): self.value = value
    def get(self) -> T: return self.value
def identity[T](value: T) -> T: return value
def first[T](values: list[T]) -> T: return values[0]
def unwrap[T](box: Box[T]) -> T: return box.value
async def later[T](value: T) -> T: return value
record = Record()
values: list[Record]
box: Box[Record]
""", expression)
    assert hint.classes == ("fixture.Record",)


def test_generic_parameter_scopes_do_not_leak() -> None:
    hint = expression_hint("""
class Record: pass
def identity[T](value: T) -> T: return value
def opaque(value: T) -> T: return value
""", "opaque(Record())")
    assert not hint


@pytest.mark.parametrize("expression", [
    "items[0]", "list(items)[0]", "next(iter(items))",
    "list(enumerate(items))[0][1]", "list(zip(items, items))[0][0]",
    "[item for item in items][0]", "next(cursor)",
])
def test_custom_generic_container_protocols(expression: str) -> None:
    hint = expression_hint("""
from collections.abc import Iterator
from typing import Self
class Record: pass
class Container[T]:
    def __getitem__(self, index: int) -> T: pass
    def __iter__(self) -> Iterator[T]: pass
class Cursor[T]:
    def __iter__(self) -> Self: return self
    def __next__(self) -> T: pass
items: Container[Record]
cursor: Cursor[Record]
""", expression)
    assert hint.classes == ("fixture.Record",)
    assert not hint.incomplete


def test_generic_async_iterator_protocol() -> None:
    hint = expression_hint("""
from typing import Self
class Record: pass
class Cursor[T]:
    def __aiter__(self) -> Self: return self
    async def __anext__(self) -> T: pass
cursor: Cursor[Record]
""", "[item async for item in cursor][0]")
    assert hint.classes == ("fixture.Record",)
    assert not hint.incomplete


@pytest.mark.parametrize("expression", [
    "items.value", "items.get()", "next(iter(items))",
    "special.value", "special.get()", "next(iter(special))", "extract(items)", "extract(special)",
])
def test_generic_inheritance_projects_type_arguments(expression: str) -> None:
    hint = expression_hint("""
from collections.abc import Iterator
class Record: pass
class Container[T]:
    value: T
    def get(self) -> T: pass
    def __iter__(self) -> Iterator[T]: pass
class Intermediate[U](Container[U]): pass
class Special(Intermediate[Record]): pass
def extract[T](container: Container[T]) -> T: return container.value
items: Intermediate[Record]
special: Special
""", expression)
    assert hint.classes == ("fixture.Record",)
    assert not hint.incomplete


def test_custom_container_protocols_preserve_opaque_alternatives() -> None:
    hint = expression_hint("""
from collections.abc import Iterator
class Record: pass
class Container[T]:
    def __iter__(self) -> Iterator[T]: pass
class Opaque: pass
items: Container[Record] | Opaque
""", "list(items)[0]")
    assert hint.incomplete


def test_custom_generic_container_method_rename_executes() -> None:
    source = """
from collections.abc import Iterator
class Record:
    def close(self): return 7
class Container[T]:
    def __init__(self, value: T): self.value = value
    def __iter__(self) -> Iterator[T]: return iter([self.value])
    def __getitem__(self, index: int) -> T: return self.value
items: Container[Record] = Container(Record())
__result__ = (items[0].close(), next(iter(items)).close(), [item.close() for item in items])
"""
    result, _ = allowlisted(source, ["f:Record:close*"])
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (7, 7, [7])


def test_imported_generic_protocol_survives_full_pipeline() -> None:
    result = obfuscate_modules({
        Path("models.py"): """
from collections.abc import Iterator
class Container[T]:
    value: T
    def __init__(self, value: T): self.value = value
    def __iter__(self) -> Iterator[T]: return iter([self.value])
    def get(self) -> T: return self.value
""",
        Path("main.py"): """
from models import Container
class Record:
    def close(self): return 7
class Records(Container[Record]): pass
items = Records(Record())
__result__ = (items.value.close(), items.get().close(), next(iter(items)).close())
""",
    }, ["f:Record:close*"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (7, 7, 7)


@pytest.mark.parametrize("call", ["choose(record, opaque())", "choose(record, *unknown)"])
def test_generic_unknown_arguments_remain_uncertain(call: str) -> None:
    hint = expression_hint("""
class Record: pass
def choose[T](first: T, second: T) -> T: return first
def opaque(): pass
record = Record()
""", call)
    assert hint.incomplete


def test_generic_keyword_argument_inference() -> None:
    hint = expression_hint("""
class Record: pass
def identity[T](*, value: T) -> T: return value
""", "identity(value=Record())")
    assert hint.classes == ("fixture.Record",)


@pytest.mark.parametrize("expression", [
    "identity(*(record,))", "identity(**{'value': record})",
    "defaulted()", "keyword_default()", "first((record, record))",
    "variadic(record, record)", "keywords(first=record, second=record)",
])
def test_generic_call_binding_composes_shapes(expression: str) -> None:
    hint = expression_hint("""
class Record: pass
record = Record()
def identity[T](value: T) -> T: return value
def defaulted[T](value: T = record) -> T: return value
def keyword_default[T](*, value: T = record) -> T: return value
def first[T](values: list[T]) -> T: return values[0]
def variadic[T](*values: T) -> T: return values[0]
def keywords[T](**values: T) -> T: return values['first']
""", expression)
    assert hint.classes == ("fixture.Record",)
    assert not hint.incomplete


@pytest.mark.parametrize("expression", [
    "identity(record, value=record)", "identity()", "identity(record, record)",
    "identity(**unknown)", "choose([record], opaque())",
    "choose([record], [opaque()])",
])
def test_invalid_or_uncertain_generic_binding_is_not_a_proof(expression: str) -> None:
    hint = expression_hint("""
class Record: pass
record = Record()
def identity[T](value: T) -> T: return value
def choose[T](first: list[T], second: list[T]) -> T: return first[0]
def opaque(): pass
""", expression)
    assert hint.incomplete


def test_generic_method_receiver_is_not_an_explicit_argument() -> None:
    hint = expression_hint("""
class Record: pass
class Helpers:
    def first[T](self, value: T) -> T: return value
""", "Helpers.first(Helpers(), Record())")
    assert hint.classes == ("fixture.Record",)


@pytest.mark.parametrize("name", ["Box", "Container", "Mapping", "Task", "Callable", "Optional", "list"])
def test_imported_generic_field_retains_type_arguments(name: str) -> None:
    models = ast.parse(f"class {name}[T]:\n    value: T\n")
    main = ast.parse(f"from models import {name}\nclass Record: pass\nbox: {name}[Record]\nresult = box.value\n")
    AnnotationIndex("models", models).apply()
    AnnotationIndex("main", main).apply()
    analysis = ArgumentRenamer({"models": models, "main": main})
    statement = main.body[-1]
    assert isinstance(statement, ast.Assign)
    assert analysis.value_hint(statement.value).classes == ("main.Record",)


@pytest.mark.parametrize("expression", ["dispatch(fetch)", "dispatch(callback)", "await dispatch_async(fetch_async)"])
def test_generic_callback_result_substitution(expression: str) -> None:
    hint = expression_hint("""
from collections.abc import Callable, Awaitable
class Record: pass
def fetch() -> Record: return Record()
async def fetch_async() -> Record: return Record()
def dispatch[T](callback: Callable[[], T]) -> T: return callback()
async def dispatch_async[T](callback: Callable[[], Awaitable[T]]) -> T: return await callback()
callback: Callable[[], Record]
""", expression)
    assert hint.classes == ("fixture.Record",)


def test_generic_async_dispatcher_infers_staticmethod_result() -> None:
    source = """
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
@dataclass(frozen=True, slots=True, kw_only=True)
class HealthStatus:
    uptime: float
class SysUtil:
    @staticmethod
    def uptime(): return 1.0
    @staticmethod
    def health() -> HealthStatus: return HealthStatus(uptime=2.0)
class Offload:
    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(function, *args, **kwargs)
@dataclass(frozen=True, slots=True)
class Offloads:
    metrics: Offload = field(default_factory=Offload)
@dataclass(frozen=True, slots=True, kw_only=True)
class Resources:
    offloads: Offloads
class Subscription:
    def __init__(self, res: Resources): self.res = res
class Api:
    def __init__(self, sub: Subscription): self.sub = sub
    async def health(self):
        status = await self.sub.res.offloads.metrics.run(SysUtil.health)
        return status.uptime, SysUtil.uptime()
async def use():
    return await Api(Subscription(Resources(offloads=Offloads()))).health()
__result__ = asyncio.run(use())
"""
    result = obfuscate_modules({Path("main.py"): source}, ["f:SysUtil:uptime"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (2.0, 1.0)


@pytest.mark.parametrize("decorator", ["", "@staticmethod", "@classmethod"])
def test_method_reference_exposes_return_hint(decorator: str) -> None:
    hint = expression_hint(f"""
class Record: pass
class Factory:
    {decorator}
    def make(self) -> Record: return Record()
""", "Factory.make")
    assert hint.origin == "callable"
    assert hint.arguments[-1].classes == ("fixture.Record",)


def test_unknown_method_decorator_does_not_expose_return_hint() -> None:
    hint = expression_hint("""
class Record: pass
def decorate(function): return opaque(function)
class Factory:
    @decorate
    def make(self) -> Record: return Record()
""", "Factory.make")
    assert not hint


def test_binding_annotation_survives_nested_factory_reassignments() -> None:
    source = """
import sqlite3
class Resource:
    def close(self): return 'owned'
def run():
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(':memory:')
        destination.close()
        destination = None
    finally:
        if destination is not None:
            destination.close()
    return Resource().close()
__result__ = run()
"""
    result = obfuscate_modules({Path("main.py"): source}, ["f:Resource:close*"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == "owned"
    assert ".close()" in result.source


def test_binding_annotation_does_not_hide_known_alternative() -> None:
    hint = expression_hint("""
class Record: pass
class Other: pass
value: Record = Record()
value = Other()
""", "value")
    assert set(hint.classes) == {"fixture.Record", "fixture.Other"}


def test_binding_annotation_does_not_hide_opaque_rebinding() -> None:
    hint = expression_hint("""
import sqlite3
value: sqlite3.Connection | None = None
if flag:
    value = opaque()
""", "value")
    assert not hint


def test_unannotated_external_factory_return_is_inferred_from_source() -> None:
    source = """
import asyncio
class Record:
    def close(self): return 7
async def connect():
    reader, writer = await asyncio.open_connection('localhost', 443)
    writer.close()
__result__ = Record().close()
"""
    result, names = allowlisted(source, ["f:Record:close*"])
    namespace: dict[str, Any] = {}
    # Only define connect; inference and this execution never open a connection.
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7
    assert "writer.close()" in result
    assert "f:Record:close*" in names


def test_custom_awaitable_generator_return_preserves_external_receiver() -> None:
    source = """
import aiosqlite
class Record:
    def close(self): return 7
async def connect():
    conn = await aiosqlite.connect(':memory:')
    try:
        return conn
    except BaseException:
        await conn.close()
        raise
__result__ = Record().close(), connect
"""
    result = obfuscate_modules({Path("main.py"): source}, ["f:Record:close*"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture; no database is opened.
    assert namespace["__result__"][0] == 7
    assert ".close()" in result.source


@pytest.mark.parametrize(("return_type", "succeeds"), [("External", True), ("Any", False)])
def test_custom_awaitable_uses_generator_return_not_yield_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, return_type: str, succeeds: bool,
) -> None:
    path = tmp_path / "awaitable_fixture.py"
    path.write_text(f"""
from collections.abc import Generator
from typing import Any
class External: pass
class Pending:
    def __await__(self) -> Generator[int, None, {return_type}]:
        raise NotImplementedError
class Unknown:
    def __await__(self) -> Generator[int, None, Any]:
        raise NotImplementedError
def make() -> Pending: return Pending()
def make_unknown() -> Unknown: return Unknown()
""", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("awaitable_fixture", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    tree = ast.parse("""
import awaitable_fixture
async def run(pending: awaitable_fixture.Pending):
    return await pending
""")
    AnnotationIndex("main", tree).apply()
    analysis = AllowlistRenamer(tree)
    awaited = next(node for node in ast.walk(tree) if isinstance(node, ast.Await))
    hint = analysis.attribute_flow.external_returns.hint(awaited)
    if succeeds:
        assert hint.classes == ("awaitable_fixture.External",)
    else:
        assert not hint
    source = """
import awaitable_fixture
class Record:
    def close(self): return 7
async def run():
    value = await awaitable_fixture.make()
    value.close()
__result__ = Record().close(), run
"""
    if succeeds:
        result, _ = allowlisted(source, ["f:Record:close*"])
        assert "value.close()" in result
    else:
        with pytest.raises(ValueError, match="Unresolved receiver"):
            allowlisted(source, ["f:Record:close*"])
    mixed = source.replace(
        "await awaitable_fixture.make()",
        "await (awaitable_fixture.make() if flag else awaitable_fixture.make_unknown())",
    )
    with pytest.raises(ValueError, match="Unresolved receiver"):
        allowlisted(mixed, ["f:Record:close*"])


def test_external_factory_can_delegate_to_native_file_open() -> None:
    source = """
from pathlib import Path
class Record:
    def close(self): return 7
def acquire(path: Path):
    handle = path.open('wb')
    try:
        handle.close()
    finally:
        pass
    return handle
__result__ = Record().close()
"""
    result, _ = allowlisted(source, ["f:Record:close*"])
    assert "handle.close()" in result


@pytest.mark.parametrize("mode", ["rb", "r"])
def test_external_stub_methods_follow_file_context_results(mode: str) -> None:
    source = f"""
from pathlib import Path
class Record:
    def count(self): return 7
def read(path):
    with Path(path).open({mode!r}) as handle:
        chunk = handle.read(100)
        return chunk.count('x' if {mode!r} == 'r' else b'x')
__result__ = Record().count(), read
"""
    result = obfuscate_modules({Path("main.py"): source}, ["f:Record:count*"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture; no file is opened.
    assert namespace["__result__"][0] == 7
    assert ".count(" in result.source


def test_external_stub_overload_unknown_argument_is_not_guessed() -> None:
    source = """
from pathlib import Path
class Record:
    def count(self): return 7
def read(path, mode):
    with Path(path).open(mode) as handle:
        chunk = handle.read(100)
        return chunk.count('x')
__result__ = Record().count(), read
"""
    with pytest.raises(ValueError, match="Unresolved receiver"):
        allowlisted(source, ["f:Record:count*"])


def test_external_stub_does_not_bypass_python_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "stub_override_fixture.py"
    path.write_text("""
import io
class Custom(io.BytesIO):
    def read(self, size=-1): return opaque()
def make() -> Custom: return Custom()
""", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("stub_override_fixture", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    source = """
import stub_override_fixture
class Record:
    def count(self): return 7
def read():
    chunk = stub_override_fixture.make().read()
    return chunk.count()
__result__ = Record().count(), read
"""
    with pytest.raises(ValueError, match="Unresolved receiver"):
        allowlisted(source, ["f:Record:count*"])


def test_native_factory_and_transparent_context_wrapper_compose() -> None:
    source = """
import contextlib
import sqlite3
class Record:
    def execute(self): return 7
def run():
    with contextlib.closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('SELECT 1')
    return Record().execute()
__result__ = run()
"""
    result, _ = allowlisted(source, ["f:Record:execute*"])
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7
    assert "connection.execute(" in result


def test_external_instance_fields_keep_constructor_types() -> None:
    source = """
import asyncio
class Record:
    def clear(self): return 7
class Gate:
    def __init__(self): self.event = asyncio.Event()
    def reset(self): self.event.clear()
gate = Gate()
gate.reset()
__result__ = Record().clear()
"""
    result, _ = allowlisted(source, ["f:Record:clear*"])
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7
    assert "self.event.clear()" in result


@pytest.mark.parametrize("descriptor", ["staticmethod", "classmethod"])
def test_aliased_descriptors_preserve_receiver_binding(descriptor: str) -> None:
    receiver = "cls, " if descriptor == "classmethod" else ""
    source = f"""
marker = {descriptor}
class Record:
    def __init__(self): self.rows = 7
class Service:
    @marker
    def read({receiver}record: Record): return record.rows
__result__ = Service().read(Record())
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:Record:rows"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_cast_retains_receiver_after_runtime_cleanup() -> None:
    source = """
from typing import cast
from aiohttp import ClientSession
class Record:
    def close(self): return 7
async def cleanup(opaque):
    session = cast(ClientSession | None, opaque)
    if session is not None:
        await session.close()
__result__ = Record().close()
"""
    result, _ = allowlisted(source, ["f:Record:close*"])
    assert "session.close()" in result
    assert "cast(" not in result


def test_cast_preserves_nested_generic_shape() -> None:
    hint = expression_hint("""
from typing import cast
class Record: pass
""", "cast(list[Record], opaque())[0]")
    assert hint.classes == ("fixture.Record",)


def test_native_stub_overloads_remain_conservative() -> None:
    stubs = StubReturns()
    ordinary = ast.parse("connect(':memory:')", mode="eval").body
    customized = ast.parse("connect(':memory:', factory=custom)", mode="eval").body
    assert isinstance(ordinary, ast.Call)
    assert isinstance(customized, ast.Call)
    assert stubs.returned("_sqlite3.connect", ordinary).classes in {("_sqlite3.Connection",), ("sqlite3.Connection",)}
    assert not stubs.returned("_sqlite3.connect", customized)
    stubs = StubReturns()
    stubs.root = None
    assert not stubs.returned("_sqlite3.connect", ordinary)


@pytest.mark.parametrize("annotation", ["IO[str]", "TextIO", "BinaryIO"])
def test_typing_io_receivers_do_not_conflict_with_internal_methods(annotation: str) -> None:
    source = f"""
from typing import IO, TextIO, BinaryIO
class Record:
    def close(self): return 7
class Holder:
    def __init__(self): self.handle: {annotation} | None = None
    def cleanup(self):
        if self.handle is not None:
            self.handle.close()
__result__ = Record().close()
"""
    result, _ = allowlisted(source, ["f:Record:close*"])
    assert "self.handle.close()" in result


def test_external_return_shapes_and_opaque_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "external_fixture.py"
    path.write_text("""
class Reader: pass
class Writer: pass
def pair():
    reader = Reader()
    writer = Writer()
    return reader, writer
def annotated() -> tuple[Reader, Writer]: return Reader(), Writer()
def uncertain(flag):
    if flag: return Reader()
    return opaque()
def recursive(): return recursive()
def shadowed(Writer): return Writer()
""", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("inference_external_fixture", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    returns = ExternalReturns(AllowlistRenamer(ast.parse("")))
    for name in ("pair", "annotated"):
        hint = returns.returned(getattr(module, name))
        assert hint.origin == "tuple"
        assert hint.element(0).classes == ("inference_external_fixture.Reader",)
        assert hint.element(1).classes == ("inference_external_fixture.Writer",)
    for name in ("uncertain", "recursive", "shadowed"):
        assert not returns.returned(getattr(module, name))


def test_snapshot_gather_zip_attribute_rename_executes() -> None:
    source = """
import asyncio
class Snapshot:
    def __init__(self, value): self.rows = [value]
async def fetch(value: int) -> Snapshot:
    return Snapshot(value)
async def run():
    panels = [1, 2]
    snapshots = await asyncio.gather(*(fetch(panel) for panel in panels), return_exceptions=True)
    result = []
    for panel, snapshot in zip(panels, snapshots, strict=True):
        if isinstance(snapshot, BaseException):
            continue
        result.extend(snapshot.rows)
    return result
__result__ = asyncio.run(run())
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:Snapshot:rows"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == [1, 2]
    assert "attr:Snapshot:rows" in result.renames

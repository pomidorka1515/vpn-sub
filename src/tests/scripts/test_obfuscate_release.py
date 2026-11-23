from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.support.paths import REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__: list[str] = []

pytest.importorskip("python_minifier")
SCRIPT = REPO_ROOT / "scripts" / "obfuscate_release.py"
SPEC = importlib.util.spec_from_file_location("obfuscate_release", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
obfuscator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(obfuscator)


def test_cleanup_precedes_minification(monkeypatch: pytest.MonkeyPatch) -> None:
    sources: list[str] = []

    def minify(source: str, **kwargs: object) -> str:
        sources.append(source)
        return source

    monkeypatch.setattr(obfuscator.python_minifier, "minify", minify)
    result = obfuscator.obfuscate(
        """
"module documentation"
from typing import Literal, TypeAlias, cast, TYPE_CHECKING
if TYPE_CHECKING:
    from missing_package import Missing
__all__ = ["run"]
__all__ += ["Other"]
type Choice = Literal["secret", "other"]
OldChoice: TypeAlias = Literal["old"]
class Other:
    "class documentation"
    field: str
def run[T](value: T, /, *, count: int = 5) -> T:
    "function documentation"
    "unused literal"
    local: int = count
    return cast(T, value), local
""",
        "fixture.py",
    )
    assert sources == [result.rstrip("\n")]
    assert "__all__" not in result
    assert "Literal" not in result
    assert "secret" not in result
    assert "documentation" not in result
    assert "missing_package" not in result
    tree = ast.parse(result)
    annotations = [node for node in ast.walk(tree) if isinstance(node, ast.AnnAssign)]
    assert not annotations
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef))
    assert function.returns is None
    assert not function.type_params
    assert not function.args.posonlyargs
    assert not function.args.kwonlyargs
    assert all(node.annotation is None for node in ast.walk(tree) if isinstance(node, ast.arg))


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

    response = from_dict(module.Response, {"obj": [{"traffic": 12}], "status": "ok"})
    assert isinstance(response.obj[0], module.Client)
    assert response.obj[0].traffic == 12


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


def test_cli_excludes_test_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text('"documentation"\nvalue: int = 5\n')
    for directory in (source / "tests", source / "package" / "tests", source / "__pycache__"):
        directory.mkdir(parents=True)
        (directory / "broken.py").write_text("not valid Python !!!")
    output = tmp_path / "output"
    subprocess.run([sys.executable, str(SCRIPT), str(source), str(output)], check=True)
    assert [path.relative_to(output) for path in output.rglob("*")] == [Path("app.min.py")]


def renamed_modules(sources: Mapping[str, str]) -> dict[str, str]:
    trees = {name: ast.parse(source) for name, source in sources.items()}
    obfuscator.ArgumentRenamer(trees).run()
    return {name: ast.unparse(tree) for name, tree in trees.items()}


def test_cross_module_keywords_and_reexports() -> None:
    results = renamed_modules(
        {
            "implementation": "def calculate(long_parameter, *, multiplier=2): return long_parameter * multiplier",
            "facade": "from implementation import calculate as exported",
            "consumer": "from facade import exported as run\nresult = run(long_parameter=3, multiplier=4)",
        }
    )
    function = ast.parse(results["implementation"]).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg != "long_parameter"
    assert function.args.kwonlyargs[0].arg != "multiplier"
    assert "long_parameter=" not in results["consumer"]
    assert "multiplier=" not in results["consumer"]
    # Execute with imports represented by the actual transformed objects.
    namespace: dict[str, Any] = {}
    exec(results["implementation"], namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    consumer = ast.parse(results["consumer"])
    consumer.body = consumer.body[1:]
    namespace["run"] = namespace["calculate"]
    exec(compile(consumer, "consumer", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 12


def test_bindings_closures_shadowing_nonlocal_and_comprehensions() -> None:
    source = """
def outer(long_parameter, *positional_values, **keyword_values):
    a = 9
    def mutate():
        nonlocal long_parameter
        long_parameter += 1
    def shadow(long_parameter=7):
        return long_parameter
    mutate()
    values = [long_parameter for long_parameter in range(2)]
    return long_parameter, shadow(), values, positional_values, keyword_values, a
result = outer(3, 4, extra=5)
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    tree = ast.parse(result)
    outer = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    assert outer.args.args[0].arg != "long_parameter"
    assert outer.args.vararg is not None
    assert outer.args.kwarg is not None
    assert outer.args.vararg.arg != "positional_values"
    assert outer.args.kwarg.arg != "keyword_values"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (4, 7, [0, 1], (4,), {"extra": 5}, 9)


def test_safe_method_calls_and_receivers() -> None:
    source = """
class Helper:
    def calculate(self, long_parameter):
        return self.finish(long_parameter=long_parameter)
    def finish(self, long_parameter):
        return long_parameter * 2
result = Helper().calculate(long_parameter=4)
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    functions = [node for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)]
    assert all(function.args.args[0].arg != "self" for function in functions)
    assert all(function.args.args[1].arg != "long_parameter" for function in functions)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


@pytest.mark.parametrize(
    "usage",
    [
        "alias = target\ncallbacks = [alias]\nresult = callbacks[0](long_parameter=3)",
        "options = {'long_parameter': 3}\nresult = target(**options)",
        "from inspect import signature\nparameters = signature(target).parameters\nresult = target(long_parameter=3)",
    ],
)
def test_escaping_and_dynamic_signatures_are_preserved(usage: str) -> None:
    result = renamed_modules(
        {"sample": f"def target(long_parameter): return long_parameter\n{usage}"}
    )["sample"]
    function = ast.parse(result).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg == "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 3


def test_decorators_inheritance_and_unknown_methods_are_preserved() -> None:
    result = renamed_modules(
        {
            "sample": """
def decorate(function): return function
@decorate
def decorated(long_parameter): return long_parameter
class Base:
    def method(self, long_parameter): return long_parameter
class Child(Base): pass
class Other:
    def unknown(self, long_parameter): return long_parameter
def invoke(receiver): return receiver.unknown(long_parameter=2)
decorated(long_parameter=1)
Base().method(long_parameter=1)
Other().unknown(long_parameter=1)
"""
        }
    )["sample"]
    for node in ast.walk(ast.parse(result)):
        if isinstance(node, ast.FunctionDef) and node.name in {"decorated", "method", "unknown"}:
            assert node.args.args[-1].arg == "long_parameter"


def test_fresh_arguments_do_not_capture_extra_keywords_or_global_names() -> None:
    result = obfuscator.obfuscate(
        """
a = 10
def calculate(long_parameter, **keyword_values):
    b = 20
    return long_parameter + a + b, keyword_values
result = calculate(long_parameter=3, c=4)
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (33, {"c": 4})


def test_lambda_variadics_and_cls() -> None:
    result = obfuscator.obfuscate(
        """
callback = lambda *positional_values, **keyword_values: (positional_values, keyword_values)
def plain(cls, *positional_values, **keyword_values):
    return cls, positional_values, keyword_values
result = plain(1, 2, extra=3), callback(4, extra=5)
""",
        "fixture.py",
    )
    tree = ast.parse(result)
    plain = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    assert plain.args.args[0].arg != "cls"
    assert plain.args.vararg is not None
    assert plain.args.kwarg is not None
    assert plain.args.vararg.arg != "positional_values"
    assert plain.args.kwarg.arg != "keyword_values"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == ((1, (2,), {"extra": 3}), ((4,), {"extra": 5}))


def test_star_imports_and_module_reflection_preserve_reexported_signatures() -> None:
    results = renamed_modules(
        {
            "implementation": "def calculate(long_parameter, *positional_values, **keyword_values): return long_parameter",
            "facade": "from implementation import calculate as exported",
            "consumer": "from facade import *\nfrom implementation import calculate\nresult = calculate(long_parameter=3)",
        }
    )
    function = ast.parse(results["implementation"]).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg == "long_parameter"
    assert function.args.vararg is not None
    assert function.args.kwarg is not None
    assert function.args.vararg.arg == "positional_values"
    assert function.args.kwarg.arg == "keyword_values"
    results = renamed_modules(
        {
            "implementation": "def calculate(long_parameter): return long_parameter",
            "facade": "from implementation import calculate as exported",
            "consumer": "import facade\nfrom implementation import calculate\nresult = calculate(long_parameter=3)\nlookup = vars(facade)",
        }
    )
    assert "long_parameter" in results["implementation"]


def test_mangled_arguments_are_preserved() -> None:
    result = obfuscator.obfuscate(
        """
class Helper:
    def calculate(self, __private_argument):
        return __private_argument * 2
result = Helper().calculate(_Helper__private_argument=4)
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


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
    subprocess.run([sys.executable, str(SCRIPT), str(source), str(output)], check=True)
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


def test_tainted_modules_preserve_imported_signatures() -> None:
    results = renamed_modules(
        {
            "implementation": "def calculate(long_parameter): return long_parameter\ncalculate(long_parameter=3)",
            "consumer": "from implementation import calculate as run\nnamespace = globals()\n",
        }
    )
    function = ast.parse(results["implementation"]).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg == "long_parameter"


def test_custom_attribute_lookup_preserves_method_signatures() -> None:
    result = renamed_modules(
        {
            "sample": """
class Helper:
    def __getattribute__(self, name):
        return object.__getattribute__(self, name)
    def calculate(self, long_parameter):
        return long_parameter
result = Helper().calculate(long_parameter=3)
"""
        }
    )["sample"]
    function = next(
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.FunctionDef) and node.name == "calculate"
    )
    assert function.args.args[1].arg == "long_parameter"


def test_recursive_and_async_keyword_calls() -> None:
    result = obfuscator.obfuscate(
        """
import asyncio
def factorial(long_parameter):
    if long_parameter < 2:
        return 1
    return long_parameter * factorial(long_parameter=long_parameter - 1)
async def double(long_parameter):
    return long_parameter * 2
result = factorial(long_parameter=4), asyncio.run(double(long_parameter=3))
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (24, 6)


@pytest.mark.parametrize(
    "expression", ["dir()", "sys._getframe().f_locals", "inspect.currentframe().f_locals"]
)
def test_frame_inspection_preserves_parameter_names(expression: str) -> None:
    result = renamed_modules(
        {
            "sample": f"""
import inspect
import sys
def calculate(long_parameter):
    return tuple({expression})
result = calculate(long_parameter=3)
"""
        }
    )["sample"]
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == ("long_parameter",)


@pytest.mark.parametrize("assignment", ["helper = Helper()", "helper: object = Helper()"])
def test_single_assignment_instances_allow_keyword_rewriting(assignment: str) -> None:
    result = obfuscator.obfuscate(
        f"""
class Helper:
    def calculate(self, long_parameter):
        return self.finish(long_parameter=long_parameter)
    def finish(self, long_parameter):
        return long_parameter * 2
def run():
    {assignment}
    return helper.calculate(long_parameter=4)
result = run()
""",
        "fixture.py",
    )
    methods = [
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.FunctionDef) and node.name in {"calculate", "finish"}
    ]
    assert all(method.args.args[1].arg != "long_parameter" for method in methods)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


@pytest.mark.parametrize(
    "usage",
    [
        "escaped = helper\ncallbacks = [escaped]",
        "def consume(value): pass\nconsume(helper)",
        "signature = helper.calculate\ncallbacks = [signature]",
        "from inspect import signature\nparameters = signature(helper.calculate).parameters",
        "method = getattr(helper, 'calculate')",
        "helper.calculate = lambda long_parameter: long_parameter",
        "helper = Helper()",
        "class Child(Helper): pass",
    ],
)
def test_instance_escapes_and_reassignments_preserve_methods(usage: str) -> None:
    result = renamed_modules(
        {
            "sample": f"""
class Helper:
    def calculate(self, long_parameter): return long_parameter * 2
helper = Helper()
{usage}
result = helper.calculate(long_parameter=4)
"""
        }
    )["sample"]
    function = next(
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.FunctionDef) and node.name == "calculate"
    )
    assert function.args.args[1].arg == "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] in {4, 8}


@pytest.mark.parametrize(
    "declaration",
    [
        "class Helper(list):",
        "def decorate(cls): return cls\n@decorate\nclass Helper:",
        "class Helper(metaclass=type):",
        "class Helper:\n    def __new__(cls): return object.__new__(cls)",
        "class Helper:\n    def __getattr__(self, name): raise AttributeError(name)",
    ],
)
def test_nonordinary_instances_preserve_methods(declaration: str) -> None:
    result = renamed_modules(
        {
            "sample": f"""
{declaration}
    def calculate(self, long_parameter): return long_parameter
helper = Helper()
result = helper.calculate(long_parameter=4)
"""
        }
    )["sample"]
    function = next(
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.FunctionDef) and node.name == "calculate"
    )
    assert function.args.args[1].arg == "long_parameter"


def test_unrelated_attributes_and_strings_do_not_block_closed_functions() -> None:
    result = obfuscator.obfuscate(
        """
class External:
    def calculate(self, long_parameter): return long_parameter + 1
def invoke(receiver): return receiver.calculate(long_parameter=2)
def calculate(long_parameter): return long_parameter * 2, 'long_parameter'
result = calculate(long_parameter=4), invoke(External())
""",
        "fixture.py",
    )
    tree = ast.parse(result)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "calculate"
    )
    assert function.args.args[0].arg != "long_parameter"
    method = next(node for node in tree.body if isinstance(node, ast.ClassDef)).body[0]
    assert isinstance(method, ast.FunctionDef)
    assert method.args.args[1].arg == "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == ((8, "long_parameter"), 3)


def test_instance_inference_cycles_are_unresolved() -> None:
    result = renamed_modules({"sample": "helper = helper()\nhelper.calculate(long_parameter=4)"})
    assert "long_parameter=4" in result["sample"]


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
    subprocess.run([sys.executable, str(SCRIPT), str(source), str(output)], check=True)
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


def flattened_modules(sources: Mapping[str, str]) -> str:
    """Test linking in isolation from the pre-existing obfuscation passes."""
    trees = {
        name: obfuscator.prepare_tree(source, name.replace(".", "/") + ".py", module=name)
        for name, source in sources.items()
    }
    return ast.unparse(obfuscator.flatten(trees))


def test_private_methods_and_resolved_aliases_are_minified() -> None:
    source = """
class Helper:
    a = 100
    def _calculate(self, long_parameter):
        return self._finish(long_parameter=long_parameter) + self.a
    @classmethod
    def _finish(cls, long_parameter):
        return cls._double(long_parameter=long_parameter)
    @staticmethod
    def _double(long_parameter):
        return long_parameter * 2
    def run(self):
        alias = self._calculate
        return alias(long_parameter=4)
helper = Helper()
result = helper.run()
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    methods = {
        node.name for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    }
    assert len(methods) == 4
    assert "run" in methods
    assert not {"_calculate", "_finish", "_double", "a"} & methods
    assert all(len(name) == 1 for name in methods - {"run"})
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 108


def test_private_async_methods_with_dynamic_keywords_are_minified() -> None:
    source = """
import asyncio
class Helper:
    async def _calculate(self, long_parameter):
        return long_parameter * 2
options = {'long_parameter': 4}
result = asyncio.run(Helper()._calculate(**options))
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    method = next(
        node for node in ast.walk(ast.parse(result)) if isinstance(node, ast.AsyncFunctionDef)
    )
    assert method.name != "_calculate"
    assert method.args.args[1].arg == "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


def test_staticmethod_self_parameter_is_not_a_known_receiver() -> None:
    source = """
class External:
    def _calculate(self): return 7
class Helper:
    def _calculate(self): return 8
    @staticmethod
    def run(self): return self._calculate()
result = Helper.run(External()), Helper()._calculate()
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    assert result.count("def _calculate(") == 2
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (7, 8)


@pytest.mark.parametrize(
    "usage",
    [
        "result = getattr(helper, '_calculate')(long_parameter=4)",
        "name = '_cal' + 'culate'\nresult = getattr(helper, name)(long_parameter=4)",
        "result = helper._calculate.__name__",
        "callbacks = [helper._calculate]\nresult = callbacks[0](long_parameter=4)",
        "escaped = [helper]\nresult = escaped[0]._calculate(long_parameter=4)",
        "def invoke(receiver): return receiver._calculate(long_parameter=4)\nresult = invoke(helper)",
        "helper._calculate = lambda long_parameter: 7\nresult = helper._calculate(long_parameter=4)",
        "result = helper.__class__.__dict__['_calculate'](helper, long_parameter=4)",
        "class Child(Helper): pass\nresult = Child()._calculate(long_parameter=4)",
    ],
)
def test_private_method_dynamic_apis_keep_their_names(usage: str) -> None:
    source = f"""
class Helper:
    def _calculate(self, long_parameter): return long_parameter * 2
helper = Helper()
{usage}
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    assert "def _calculate(" in result
    original: dict[str, Any] = {}
    release: dict[str, Any] = {}
    exec(source, original)  # ruff: ignore[exec-builtin] - Fixed fixture.
    exec(result, release)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert original["result"] == release["result"]


@pytest.mark.parametrize(
    "definition",
    [
        "class Helper(list):\n def _calculate(self, value): return value * 2",
        "class Helper:\n def __getattribute__(self, name): return object.__getattribute__(self, name)\n def _calculate(self, value): return value * 2",
        "class Helper:\n @property\n def _calculate(self): return lambda value: value * 2",
        "class Helper:\n def __calculate__(self, value): return value * 2\n _calculate = __calculate__",
    ],
)
def test_private_method_special_classes_and_descriptors_are_preserved(definition: str) -> None:
    source = definition + "\nresult = Helper()._calculate(4)\n"
    result = obfuscator.obfuscate(source, "fixture.py")
    assert "_calculate" in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


def test_linked_globals_imports_and_private_methods_are_minified(tmp_path: Path) -> None:
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
    subprocess.run([sys.executable, str(SCRIPT), str(source), str(output)], check=True)
    result = (output / "app.min.py").read_text()
    assert "long_counter" not in result
    assert "square_root" not in result
    assert "_calculate" not in result
    assert "class Helper:" in result
    assert "def increment(" in result
    execution = subprocess.run(
        [sys.executable, str(output / "app.min.py")], check=True, capture_output=True, text=True
    )
    assert execution.stdout.strip() == "(4.0, 5, 99)"


@pytest.mark.parametrize("rename_globals", [False, True])
def test_minification_never_aliases_builtins(
    rename_globals: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    source = """
from math import sqrt as square_root
long_global = 7
def run():
    values = [int(value) for value in range(4)]
    sizes = len(values), len(values), len(values)
    valid = isinstance(values, list), isinstance(values, list)
    return sizes, valid, sum(values), square_root(long_global + 2)
print(run())
"""
    tree = obfuscator.prepare_tree(source, "fixture.py")
    result = obfuscator.minify_tree(tree, "fixture.py", rename_globals=rename_globals)
    minified = ast.parse(result)
    builtin_calls = {"int", "range", "len", "isinstance", "sum", "print"}
    assert builtin_calls <= {
        node.func.id
        for node in ast.walk(minified)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id in builtin_calls | {"list"}
        for node in ast.walk(minified)
    )
    if rename_globals:
        assert "long_global" not in result
        assert "square_root" not in result
    exec(result, {})  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert capsys.readouterr().out == "((4, 4, 4), (True, True), 6, 3.0)\n"


def test_global_minification_preserves_annotations_and_callable_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = """
from __future__ import annotations
from dataclasses import dataclass, InitVar as Initial, KW_ONLY as KeywordOnly
from typing import ClassVar as Shared
from dacite import from_dict
Alias = list[int]
@dataclass
class Record:
    shared: Shared[int] = 3
    value: 'Alias'
    _: KeywordOnly
    initial: Initial[int] = 4
    def __post_init__(self, initial):
        self.value.append(initial)
long_global = 7
def callback(value): return value
print(from_dict(Record, {'value': [1]}).value, Record.__name__, callback.__name__, long_global)
"""
    tree = obfuscator.prepare_tree(source, "fixture.py")
    result = obfuscator.minify_tree(tree, "fixture.py", rename_globals=True)
    assert "long_global" not in result
    module = types.ModuleType("minified_globals_annotations_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(result, module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert capsys.readouterr().out == "[1, 4] Record callback 7\n"
    assert module.Record.__annotations__["value"] == "'Alias'"
    assert module.callback.__name__ == "callback"


def test_global_minification_keeps_dynamic_namespaces(capsys: pytest.CaptureFixture[str]) -> None:
    source = "long_global = 7\nprint(globals()['long_global'])\n"
    tree = obfuscator.prepare_tree(source, "fixture.py")
    result = obfuscator.minify_tree(tree, "fixture.py", rename_globals=True)
    assert "long_global =" in ast.unparse(ast.parse(result))
    exec(result, {})  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert capsys.readouterr().out == "7\n"


@pytest.mark.parametrize(
    "expression",
    ["sys._getframe().f_globals['long_global']", "inspect.currentframe().f_globals['long_global']"],
)
def test_global_minification_keeps_frame_visible_names(
    expression: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = f"import sys, inspect\nlong_global = 7\nprint({expression})\n"
    tree = obfuscator.prepare_tree(source, "fixture.py")
    result = obfuscator.minify_tree(tree, "fixture.py", rename_globals=True)
    exec(result, {})  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert capsys.readouterr().out == "7\n"


def test_flatten_links_modules_and_reexports_without_namespaces() -> None:
    result = flattened_modules(
        {
            "worker": "def calculate(value): return value * 3\n",
            "facade": "from worker import calculate as exported\n",
            "main": "import worker\nimport facade as f\nresult = worker.calculate(2), f.exported(4)\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (6, 12)
    assert not any(isinstance(node, ast.Attribute) for node in ast.walk(ast.parse(result)))
    assert "import worker" not in result
    assert "import facade" not in result


def test_flatten_isolates_colliding_globals_imports_and_shadowed_locals() -> None:
    result = flattened_modules(
        {
            "first": "import math as helper\nvalue = 9\ndef read(): return helper.sqrt(value)\n",
            "second": "from math import floor as helper\nvalue = 4.5\ndef read(): return helper(value)\n",
            "main": "import first\nimport second\nmath = 'not a library'\n"
            "def local(first): return first.read()\n"
            "result = first.read(), second.read(), local(type('Receiver', (), {'read': lambda self: 7})())\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (3, 4, 7)
    imports = [
        node for node in ast.parse(result).body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    aliases = [
        alias.asname
        for node in imports
        if not isinstance(node, ast.ImportFrom) or node.module != "__future__"
        for alias in node.names
    ]
    assert "helper" in aliases
    assert any(alias and alias.startswith("_e") for alias in aliases)


def test_flatten_rewrites_module_global_writes_and_global_declarations() -> None:
    result = flattened_modules(
        {
            "worker": "value = 1\ndef increment():\n global value\n value += 1\n",
            "main": "import worker\nvalue = 99\nworker.value = 4\nworker.increment()\nresult = worker.value, value\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (5, 99)


def test_flatten_keeps_runtime_forward_annotations_bound_to_original_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = flattened_modules(
        {
            "models": "from dataclasses import dataclass\n@dataclass\nclass Item:\n value: int\n"
            "@dataclass\nclass Record:\n item: 'Item'\n",
            "main": "class Item: pass\nfrom models import Record\n"
            "from dacite import from_dict\nresult = from_dict(Record, {'item': {'value': 3}}).item.value\n",
        }
    )
    module = types.ModuleType("flattened_annotations_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(result, module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert module.result == 3


def test_flatten_probe_drops_only_dynamic_internal_import_checks() -> None:
    result = flattened_modules(
        {
            "worker": "value = 3\n",
            "main": "from importlib import import_module\n"
            "for module in ('worker', 'math'): import_module(module)\n"
            "result = ('worker', 'keep this string')\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == ("worker", "keep this string")


def test_flatten_preserves_dotted_imports_and_does_not_shadow_builtins() -> None:
    result = flattened_modules(
        {
            "package": "",
            "package.worker": "len = 7\ndef read(): return len\n",
            "main": "import package.worker\nimport xml.etree.ElementTree\n"
            "result = package.worker.read(), len([1, 2]), xml.etree.ElementTree.fromstring('<x/>').tag\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (7, 2, "x")


def test_flatten_preserves_noncolliding_names_and_reexports() -> None:
    result = flattened_modules(
        {
            "worker": "import math as maths\n"
            "class Calculator:\n def calculate(self, long_parameter):\n"
            "  local_value = maths.sqrt(long_parameter)\n  return local_value\n"
            "def calculate(long_parameter):\n"
            " local_result = Calculator().calculate(long_parameter=long_parameter)\n"
            " return local_result\n",
            "facade": "from worker import calculate\n",
            "main": "from facade import calculate\nresult = calculate(long_parameter=9)\n",
        }
    )
    tree = ast.parse(result)
    assert not any(
        isinstance(node, ast.Name) and node.id.startswith(("_g", "_e")) for node in ast.walk(tree)
    )
    assert {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)} == {
        "Calculator"
    }
    assert {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)} == {
        "self",
        "long_parameter",
    }
    assert {"maths", "local_value", "local_result"} <= {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 3
    assert namespace["calculate"].__name__ == "calculate"


@pytest.mark.parametrize("minified", [False, True])
def test_flatten_preserves_colliding_public_class_names(minified: bool) -> None:
    result = flattened_modules(
        {
            "first": "class Helper:\n value = 1\n class Nested: pass\n",
            "second": "seen = []\n"
            "def decorate(cls):\n seen.append((cls.__name__, cls.__qualname__)); return cls\n"
            "@decorate\nclass Helper:\n value = 2\n class Nested: pass\n",
            "main": "import first\nimport second\n"
            "__result__ = (first.Helper.value, second.Helper.value, "
            "first.Helper.__name__, second.Helper.__name__, "
            "first.Helper.Nested.__qualname__, second.Helper.Nested.__qualname__, second.seen)\n",
        }
    )
    classes = [node for node in ast.parse(result).body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["Helper", "Helper"]
    if minified:
        tree = ast.parse(result)
        obfuscator.PrivateMethodRenamer({"__main__": tree}).run()
        obfuscator.ArgumentRenamer({"__main__": tree}).run()
        result = obfuscator.minify_tree(tree, "fixture.py", rename_globals=True)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (
        1,
        2,
        "Helper",
        "Helper",
        "Helper.Nested",
        "Helper.Nested",
        [("Helper", "Helper")],
    )


def test_flatten_preserves_public_class_names_despite_shadowing_and_other_globals() -> None:
    result = flattened_modules(
        {
            "data": "Helper = 7\n",
            "worker": "class Helper: pass\nclass Service: pass\n",
            "main": "import data\nimport worker\n"
            "def invoke(Service): return worker.Service.__name__, Service\n"
            "result = data.Helper, worker.Helper.__name__, invoke('local')\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (7, "Helper", ("Service", "local"))


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
        result = obfuscator.minify_tree(ast.parse(source), "fixture.py", rename_globals=True)
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
    result = obfuscator.minify_tree(ast.parse(result), "fixture.py", rename_globals=True)
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


def test_flatten_renames_only_one_of_two_distinct_colliding_definitions() -> None:
    result = flattened_modules(
        {
            "first": "def calculate(value): return value + 1\n",
            "second": "def calculate(value): return value + 2\n",
            "main": "import first\nimport second\nresult = first.calculate(1), second.calculate(1)\n",
        }
    )
    functions = [node for node in ast.parse(result).body if isinstance(node, ast.FunctionDef)]
    assert sum(node.name == "calculate" for node in functions) == 1
    assert all(node.args.args[0].arg == "value" for node in functions)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (2, 3)


def test_flatten_avoids_capture_by_callers_local_names() -> None:
    result = flattened_modules(
        {
            "worker": "from math import sqrt\ndef calculate(value): return sqrt(value)\n",
            "main": "import worker\ndef invoke(calculate, sqrt):\n"
            " return worker.calculate(9), worker.sqrt(16), calculate, sqrt\n"
            "result = invoke('local function name', 'local import name')\n",
        }
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (3, 4, "local function name", "local import name")
    invoke = next(
        node
        for node in ast.parse(result).body
        if isinstance(node, ast.FunctionDef) and node.name == "invoke"
    )
    assert [arg.arg for arg in invoke.args.args] == ["calculate", "sqrt"]


def test_standalone_obfuscation_keeps_existing_argument_renaming() -> None:
    result = obfuscator.obfuscate(
        "def calculate(long_parameter):\n local_result = long_parameter + 1\n return local_result\n"
        "result = calculate(long_parameter=3)\n",
        "fixture.py",
    )
    function = next(node for node in ast.parse(result).body if isinstance(node, ast.FunctionDef))
    assert function.name == "calculate"
    assert function.args.args[0].arg != "long_parameter"
    assert "long_parameter=" not in result


def test_removed_local_import_alias_is_not_a_collision() -> None:
    result = flattened_modules(
        {
            "worker": "def calculate(value): return value + 1\n",
            "main": "def invoke():\n import worker as calculate\n"
            " return calculate.calculate(2)\nresult = invoke()\n",
        }
    )
    assert "def calculate(value)" in result
    assert "_g" not in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 3


@pytest.mark.parametrize(
    "usage",
    [
        "result = helper().calculate(long_parameter=4)",
        "returned = helper()\nresult = returned.calculate(long_parameter=4)",
    ],
)
def test_callable_instances_are_not_treated_as_constructors(usage: str) -> None:
    result = obfuscator.obfuscate(
        f"""
class Other:
    def calculate(self, long_parameter): return long_parameter * 2
class Helper:
    def __call__(self): return Other()
    def calculate(self, long_parameter): return long_parameter
helper = Helper()
{usage}
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


@pytest.mark.parametrize(
    "definition",
    [
        "def target(long_parameter): return long_parameter * 2",
        "target = lambda long_parameter: long_parameter * 2",
    ],
)
def test_closed_alias_chains_and_literal_keyword_dicts(definition: str) -> None:
    result = obfuscator.obfuscate(
        f"""
{definition}
alias = target
other: object = alias
result = other(**{{'long_parameter': 4}})
""",
        "fixture.py",
    )
    functions = [
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, (ast.FunctionDef, ast.Lambda))
    ]
    assert functions[0].args.args[0].arg != "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


@pytest.mark.parametrize(
    "usage",
    [
        "from inspect import signature\nparameters = signature(alias).parameters",
        "callbacks = [alias]",
        "alias = lambda long_parameter: long_parameter",
    ],
)
def test_alias_escapes_and_reassignment_preserve_signatures(usage: str) -> None:
    result = renamed_modules(
        {
            "sample": f"""
def target(long_parameter): return long_parameter
alias = target
{usage}
result = alias(long_parameter=3)
"""
        }
    )["sample"]
    function = ast.parse(result).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg == "long_parameter"


def test_closed_descriptors_constructor_and_data_attributes() -> None:
    result = obfuscator.obfuscate(
        """
class Helper:
    factor = 2
    def __init__(self, initial_value):
        self.value = initial_value
    @staticmethod
    def calculate(long_parameter):
        return long_parameter * Helper.factor
    @classmethod
    def finish(cls, long_parameter):
        return cls.calculate(long_parameter=long_parameter)
    def run(self, long_parameter):
        return self.finish(long_parameter=self.value + long_parameter)
Alias = Helper
helper = Alias(initial_value=3)
instance_alias = helper
callback = instance_alias.run
result = callback(long_parameter=4)
""",
        "fixture.py",
    )
    functions = [node for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)]
    assert len(functions) == 4
    assert all(
        function.args.args[-1].arg not in {"initial_value", "long_parameter"}
        for function in functions
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 14


def test_shadowed_method_descriptor_is_not_trusted() -> None:
    result = renamed_modules(
        {
            "sample": """
def staticmethod(function): return function
class Helper:
    @staticmethod
    def calculate(long_parameter): return long_parameter
result = Helper.calculate(long_parameter=4)
"""
        }
    )["sample"]
    function = next(
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.FunctionDef) and node.name == "calculate"
    )
    assert function.args.args[0].arg == "long_parameter"


def test_literal_keywords_preserve_extras_duplicates_and_evaluation_order() -> None:
    result = obfuscator.obfuscate(
        """
events = []
def mark(value):
    events.append(value)
    return value
def target(long_parameter, **options):
    return long_parameter, options
result = target(**{'long_parameter': mark(1), 'a': mark(2), 'long_parameter': mark(3)})
try:
    target(long_parameter=4, **{'long_parameter': 5})
except TypeError:
    duplicate_rejected = True
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (3, {"a": 2})
    assert namespace["events"] == [1, 2, 3]
    assert namespace["duplicate_rejected"] is True


@pytest.mark.parametrize("expression", ["{**options}", "{key: 3}"])
def test_partly_dynamic_keyword_dictionaries_preserve_signatures(expression: str) -> None:
    result = renamed_modules(
        {
            "sample": f"""
def target(long_parameter): return long_parameter
options = {{'long_parameter': 3}}
key = 'long_parameter'
result = target(**{expression})
"""
        }
    )["sample"]
    function = ast.parse(result).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg == "long_parameter"


def test_short_names_are_reused_without_capturing_closures_or_keywords() -> None:
    result = obfuscator.obfuscate(
        """
def first(long_parameter): return long_parameter
def second(other_parameter): return other_parameter
def outer(outer_parameter):
    def inner(inner_parameter): return outer_parameter + inner_parameter
    return inner(inner_parameter=2)
result = first(long_parameter=3), second(other_parameter=4), outer(outer_parameter=5)
""",
        "fixture.py",
    )
    functions = {
        node.name: node for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    }
    assert functions["first"].args.args[0].arg == "a"
    assert functions["second"].args.args[0].arg == "a"
    assert functions["outer"].args.args[0].arg != functions["inner"].args.args[0].arg
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (3, 4, 7)


def test_cross_module_aliases_are_followed() -> None:
    results = renamed_modules(
        {
            "implementation": "def target(long_parameter): return long_parameter\nexported = target",
            "consumer": "from implementation import exported\nalias = exported\nresult = alias(long_parameter=3)",
        }
    )
    assert "long_parameter" not in results["implementation"]
    assert "long_parameter=" not in results["consumer"]


def test_tainted_reexport_of_lambda_preserves_signature() -> None:
    results = renamed_modules(
        {
            "implementation": "target = lambda long_parameter: long_parameter",
            "facade": "from implementation import target as exported\nnamespace = globals()",
            "consumer": "from implementation import target\nresult = target(long_parameter=3)",
        }
    )
    assert "long_parameter" in results["implementation"]
    assert "long_parameter=3" in results["consumer"]


def test_callable_instance_keywords_are_not_constructor_keywords() -> None:
    result = obfuscator.obfuscate(
        """
class Helper:
    def __init__(self, long_parameter): self.value = long_parameter
    def __call__(self, long_parameter): return self.value + long_parameter
helper = Helper(long_parameter=3)
result = helper(long_parameter=4)
""",
        "fixture.py",
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 7


def test_external_reexport_does_not_expose_other_module_signatures() -> None:
    results = renamed_modules(
        {
            "worker": "from math import sqrt\ndef calculate(long_parameter): return long_parameter * 2",
            "consumer": "import worker\nresult = worker.calculate(long_parameter=worker.sqrt(16))",
        }
    )
    assert "long_parameter" not in results["worker"]
    assert "long_parameter=" not in results["consumer"]


@pytest.mark.parametrize("usage", ["worker.__dict__", "vars(worker)", "worker.sqrt = replacement"])
def test_module_reflection_and_attribute_writes_still_preserve_signatures(usage: str) -> None:
    results = renamed_modules(
        {
            "worker": "from math import sqrt\ndef calculate(long_parameter): return long_parameter",
            "consumer": f"import worker\n{usage}\nworker.calculate(long_parameter=3)",
        }
    )
    assert "long_parameter" in results["worker"]


def test_unresolved_local_names_do_not_block_unrelated_functions() -> None:
    result = renamed_modules(
        {
            "sample": """
def calculate(long_parameter): return long_parameter * 2
def unrelated(calculate): return calculate + 1
result = calculate(long_parameter=4), unrelated(2)
"""
        }
    )["sample"]
    function = ast.parse(result).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert function.args.args[0].arg != "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == (8, 3)


@pytest.mark.parametrize(
    ("imports", "decorator"),
    [
        ("from contextlib import contextmanager", "contextmanager"),
        ("from contextlib import contextmanager as cm", "cm"),
        ("import contextlib as cl", "cl.contextmanager"),
    ],
)
def test_contextmanager_decorators_forward_rewritten_keywords(imports: str, decorator: str) -> None:
    result = obfuscator.obfuscate(
        f"""
{imports}
@{decorator}
def resource(long_parameter):
    yield long_parameter * 2
with resource(long_parameter=4) as value:
    result = value
""",
        "fixture.py",
    )
    function = next(
        node for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    )
    assert function.args.args[0].arg != "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


def test_asynccontextmanager_forwards_rewritten_keywords() -> None:
    result = obfuscator.obfuscate(
        """
import asyncio
from contextlib import asynccontextmanager
@asynccontextmanager
async def resource(long_parameter):
    yield long_parameter * 2
async def run():
    async with resource(long_parameter=4) as value:
        return value
result = asyncio.run(run())
""",
        "fixture.py",
    )
    function = next(
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "resource"
    )
    assert function.args.args[0].arg != "long_parameter"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


def test_shadowed_contextmanager_is_not_trusted() -> None:
    result = renamed_modules(
        {
            "sample": """
from contextlib import contextmanager
def contextmanager(function): return function
@contextmanager
def resource(long_parameter): return long_parameter
result = resource(long_parameter=4)
"""
        }
    )["sample"]
    function = next(
        node
        for node in ast.walk(ast.parse(result))
        if isinstance(node, ast.FunctionDef) and node.name == "resource"
    )
    assert function.args.args[0].arg == "long_parameter"


def test_escaping_unbound_receiver_keywords_are_preserved() -> None:
    result = obfuscator.obfuscate(
        """
class Helper:
    def calculate(self, long_parameter): return long_parameter * 2
helper = Helper()
result = Helper.calculate(self=helper, long_parameter=4)
""",
        "fixture.py",
    )
    # Passing the instance as a value is still an escape; preserve this API.
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


def test_reflected_contextmanager_keeps_its_signature() -> None:
    result = renamed_modules(
        {
            "sample": """
from contextlib import contextmanager
from inspect import signature
@contextmanager
def resource(long_parameter): yield long_parameter
parameters = signature(resource).parameters
with resource(long_parameter=4): pass
"""
        }
    )["sample"]
    assert "long_parameter" in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert list(namespace["parameters"]) == ["long_parameter"]


def test_staged_contextlib_module_is_not_trusted_as_stdlib() -> None:
    results = renamed_modules(
        {
            "contextlib": "def contextmanager(function): return function",
            "sample": """
from contextlib import contextmanager
@contextmanager
def resource(long_parameter): return long_parameter
result = resource(long_parameter=4)
""",
        }
    )
    assert "def resource(long_parameter)" in results["sample"]


def test_cli_reports_parameter_rename_counts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text(
        "def calculate(long_parameter): return long_parameter\n"
        "def external(public_parameter): return public_parameter\n"
        "result = calculate(long_parameter=4)\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(tmp_path / "output"), "--report"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Renamed 1/2 parameters" in result.stdout
    assert "Preserved 1: no resolved calls" in result.stdout

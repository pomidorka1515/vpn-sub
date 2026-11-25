from __future__ import annotations

import ast
from typing import Any

import pytest

pytest.importorskip("python_minifier")

import obfuscator
from obfuscator.pipeline import prepare_tree
from obfuscator.renamers.base import ArgumentRenamer
from tests.support.obfuscator import renamed_modules

__all__: list[str] = []


def test_hint_cache_keeps_recursive_and_independent_lookups_separate() -> None:
    tree = prepare_tree("class Box: pass\nresult = (Box(),)[0]\n", "fixture.py")
    analysis = ArgumentRenamer({"__main__": tree})
    owner, assignment = tree.body
    assert isinstance(owner, ast.ClassDef)
    assert isinstance(assignment, ast.Assign)
    expression = assignment.value
    assert isinstance(expression, ast.Subscript)
    assert not analysis.type_targets(expression, {id(expression.value)})
    assert analysis.type_targets(expression) == {owner}
    assert not analysis.type_targets(expression, {id(expression.value)})


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

from __future__ import annotations

import ast
import sys
import types
from typing import Any

import pytest

pytest.importorskip("python_minifier")

from obfuscator.pipeline import minify_tree
from obfuscator.renamers.base import ArgumentRenamer
from obfuscator.renamers.methods import PrivateMethodRenamer
from tests.support.obfuscator import flattened_modules

__all__: list[str] = []


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
        PrivateMethodRenamer({"__main__": tree}).run()
        ArgumentRenamer({"__main__": tree}).run()
        result = minify_tree(tree, "fixture.py", rename_globals=True)
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

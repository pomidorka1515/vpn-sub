from __future__ import annotations

import ast
from typing import Any

import pytest

pytest.importorskip("python_minifier")

import obfuscator

__all__: list[str] = []


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


def test_dataclass_classes_keep_closed_private_methods_minified() -> None:
    source = """
from dataclasses import dataclass
@dataclass
class Database:
    def _query(self, long_parameter):
        return long_parameter * 2
@dataclass
class Service:
    db: Database
    def run(self):
        return self.db._query(long_parameter=4)
result = Service(Database()).run()
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    methods = {
        node.name for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    }
    assert "_query" not in methods
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


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

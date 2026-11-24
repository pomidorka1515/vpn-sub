from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from obfuscator import no_rename, no_rename_arg, obfuscate
from obfuscator.pipeline import obfuscate_modules, prepare_tree

if TYPE_CHECKING:
    from collections.abc import Callable

__all__: list[str] = []


def test_runtime_markers_are_identity_decorators() -> None:
    def regular(value: int) -> int:
        return value

    async def asynchronous(value: int) -> int:
        return value

    class Record:
        pass

    assert no_rename(regular) is regular
    assert no_rename(asynchronous) is asynchronous
    assert no_rename(Record) is Record
    protect: Callable[[Callable[[int], int]], Callable[[int], int]] = no_rename_arg({"value"})
    assert protect(regular) is regular
    assert no_rename_arg({"*"})(asynchronous) is asynchronous


def test_importing_markers_does_not_load_build_dependencies() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from obfuscator import no_rename, no_rename_arg; import sys; "
                "assert 'python_minifier' not in sys.modules; "
                "assert 'obfuscator.pipeline' not in sys.modules; "
                "assert 'sqlglot' not in sys.modules"
            ),
        ],
        check=True,
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("whole_program", [False, True])
def test_markers_protect_names_and_selected_arguments(
    asynchronous: bool, whole_program: bool
) -> None:
    source = f"""
from obfuscator import no_rename, no_rename_arg as keep
@no_rename
class _ImportantClass:
    @keep({{'important_argument'}})
    def __init__(self, important_argument, ordinary_argument):
        self.value = important_argument + ordinary_argument
    @no_rename
    def _important_method(self): return self.value
@no_rename
@keep(['important_argument'])
{'async ' if asynchronous else ''}def important_function(important_argument, ordinary_argument):
    return important_argument + ordinary_argument
__instance__ = _ImportantClass(important_argument=2, ordinary_argument=3)
__result__ = __instance__._important_method()
__call__ = important_function(important_argument=1, ordinary_argument=4)
"""
    result = (
        obfuscate_modules({Path("main.py"): source}, [], jobs=1).source
        if whole_program
        else obfuscate(source, "main.py")
    )
    assert "obfuscator" not in result
    tree = ast.parse(result)
    definitions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"_ImportantClass", "_important_method", "important_function"} <= definitions.keys()
    for name in ("__init__", "important_function"):
        function = definitions[name]
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        arguments = [arg.arg for arg in function.args.args]
        assert "important_argument" in arguments
        assert "ordinary_argument" not in arguments


def test_wildcard_covers_every_parameter_kind() -> None:
    source = """
import obfuscator as hints
@hints.no_rename_arg({'*'})
async def helper(positional, /, normal, *variadic, keyword, **keywords):
    return positional, normal, variadic, keyword, keywords
__result__ = helper(1, normal=2, keyword=3)
"""
    result = obfuscate_modules({Path("main.py"): source}, [], jobs=1)
    function = next(n for n in ast.walk(ast.parse(result.source)) if isinstance(n, ast.AsyncFunctionDef))
    assert {arg.arg for arg in ast.walk(function.args) if isinstance(arg, ast.arg)} == {
        "positional", "normal", "variadic", "keyword", "keywords"
    }
    assert "obfuscator" not in result.source


def test_stacked_decorators_remain_in_order() -> None:
    tree = prepare_tree(
        """
from obfuscator import no_rename, no_rename_arg
@outer
@no_rename
@middle
@no_rename_arg(('value',))
@inner
def helper(value): return value
""",
        "fixture.py",
    )
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef))
    assert [ast.unparse(d) for d in function.decorator_list] == ["outer", "middle", "inner"]
    assert function.__dict__["no_rename"]
    assert function.args.args[0].__dict__["no_rename"]


def test_marker_warnings_are_nonfatal(capsys: pytest.CaptureFixture[str]) -> None:
    result = obfuscate_modules(
        {Path("main.py"): """
from obfuscator import no_rename, no_rename_arg
@no_rename
class ImportantClass: pass
@no_rename
@no_rename_arg({'*', 'value', 'missing'})
def helper(value): return value
__result__ = helper(value=3)
"""},
        ["c:ImportantClass", "f:helper", "a:helper:value"],
        jobs=1,
    )
    warnings = capsys.readouterr().err
    assert "mixes '*'" in warnings
    assert "'missing' does not exist" in warnings
    assert warnings.count("rule ignored") == 3
    assert result.renames == {}


def test_overlapping_star_rule_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    result = obfuscate_modules(
        {Path("main.py"): """
from obfuscator import no_rename_arg
@no_rename_arg({'value'})
def helper(value): return value
__result__ = helper(value=3)
"""},
        ["f:helper*"],
        jobs=1,
    )
    assert result.renames == {}
    assert "rule ignored" in capsys.readouterr().err


def test_same_named_protected_definitions_keep_runtime_names() -> None:
    result = obfuscate_modules(
        {
            Path("one.py"): "from obfuscator import no_rename\n@no_rename\ndef helper(): return 1\n",
            Path("two.py"): "from obfuscator import no_rename\n@no_rename\ndef helper(): return 2\n",
            Path("main.py"): """
from one import helper as first
from two import helper as second
__result__ = first(), second(), first.__name__, second.__name__
""",
        },
        ["f:one.py:helper", "f:two.py:helper"],
        jobs=1,
    )
    namespace: dict[str, object] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (1, 2, "helper", "helper")


def test_shadowed_marker_is_not_stripped() -> None:
    tree = prepare_tree(
        """
from obfuscator import no_rename
def outer(no_rename):
    @no_rename
    def nested(): pass
    return nested
""",
        "fixture.py",
    )
    nested = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "nested")
    assert [ast.unparse(d) for d in nested.decorator_list] == ["no_rename"]


def test_stripping_markers_keeps_unrelated_side_effect_imports() -> None:
    tree = prepare_tree(
        "import side_effects\nfrom obfuscator import no_rename\n@no_rename\ndef helper(): pass\n",
        "fixture.py",
    )
    assert any(isinstance(n, ast.Import) and n.names[0].name == "side_effects" for n in tree.body)


def test_folded_mixin_methods_retain_protections(capsys: pytest.CaptureFixture[str]) -> None:
    result = obfuscate_modules(
        {
            Path("db/features.py"): """
from obfuscator import no_rename, no_rename_arg
class FeatureMixin:
    @no_rename
    @no_rename_arg({'value'})
    def _important_method(self, value): return value
""",
            Path("db/database.py"): """
from .features import FeatureMixin
class Database(FeatureMixin): pass
__result__ = Database()._important_method(value=3)
""",
        },
        ["f:Database:_important_method", "a:Database:_important_method:value"],
        jobs=1,
    )
    assert result.renames == {}
    assert capsys.readouterr().err.count("rule ignored") == 2
    namespace: dict[str, object] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 3


@pytest.mark.parametrize(
    ("decorator", "definition"),
    [
        ("no_rename()", "def helper(): pass"),
        ("no_rename_arg({'value'})", "class Helper: pass"),
        ("no_rename_arg({'value': 1})", "def helper(value): pass"),
        ("no_rename_arg(get_names())", "def helper(value): pass"),
    ],
)
def test_invalid_markers_fail_without_executing_arguments(decorator: str, definition: str) -> None:
    with pytest.raises(ValueError, match="no_rename"):
        prepare_tree(
            f"from obfuscator import no_rename, no_rename_arg\n@{decorator}\n{definition}\n",
            "fixture.py",
        )

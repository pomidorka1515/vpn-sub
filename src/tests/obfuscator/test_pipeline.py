from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("python_minifier")

import python_minifier

import obfuscator
from obfuscator import pipeline
from obfuscator.analysis import inspects_frames
from obfuscator.pipeline import minify_tree, obfuscate_modules, prepare_tree
from obfuscator.renamers.allowlist import AllowlistRenamer
from obfuscator.renamers.methods import PrivateMethodRenamer

__all__: list[str] = []


@pytest.mark.parametrize(
    "source",
    [
        "x = 1\n",
        "import sys\nsys._getframe()\n",
        "from sys import settrace as trace\n",
        "frame.f_locals\n",
        "dir()\n",
        "dir(value)\n",
        "dir(object=value)\n",
    ],
)
def test_combined_metadata_frame_check_matches_original(source: str) -> None:
    tree = ast.parse(source)
    assert pipeline._minify_metadata(tree)[1] == inspects_frames(tree)


def test_pipeline_links_modules_and_reports_renames() -> None:
    result = obfuscate_modules(
        {
            Path("worker.py"): "def helper(value): return value + 1\n",
            Path("main.py"): "from worker import helper\n__result__ = helper(value=4)\n",
        },
        ["f:helper"],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 5
    assert set(result.renames) == {"f:helper"}
    assert result.renamed_parameters == 1
    assert result.skipped_parameters.total() == 0
    assert result.renamed_methods == 0


def test_pipeline_rejects_empty_sources() -> None:
    with pytest.raises(ValueError, match="No application Python modules found"):
        obfuscate_modules({}, [])


def test_pipeline_rejects_ambiguous_module_names() -> None:
    with pytest.raises(ValueError, match="ambiguous module name: worker"):
        obfuscate_modules({Path("worker.py"): "", Path("worker/__init__.py"): ""}, [])


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (["f:missing"], "target not found"),
        (["c:helper"], "incorrect type"),
        (["attr:Missing:value"], "target not found"),
        (["f:helper*"], "Escaping callable"),
        (["a:helper:value"], "Escaping callable"),
    ],
)
def test_allowlist_errors_precede_expensive_renaming(
    entries: list[str], message: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dataclasses(tree: ast.Module) -> None:
        pytest.fail("Invalid allowlist reached dataclass analysis")

    monkeypatch.setattr(pipeline, "minify_dataclasses", unexpected_dataclasses)
    with pytest.raises(ValueError, match=message):
        obfuscate_modules(
            {Path("main.py"): "def helper(value): return value\nprint(helper)\n"},
            entries,
            jobs=1,
        )


@pytest.mark.parametrize(
    ("use", "message"),
    [
        ("alias = helper\nprint(alias)\n", "Escaping callable"),
        ("__result__ = helper(**external())\n", r"Dynamic \*\*kwargs"),
    ],
)
def test_parameter_preflight_follows_aliases_and_dynamic_keywords(
    use: str, message: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dataclasses(tree: ast.Module) -> None:
        pytest.fail("Unsafe parameter allowlist reached dataclass analysis")

    monkeypatch.setattr(pipeline, "minify_dataclasses", unexpected_dataclasses)
    with pytest.raises(ValueError, match=message):
        obfuscate_modules(
            {Path("main.py"): "def helper(value): return value\n" + use},
            ["f:helper*"],
            jobs=1,
        )


def test_parameter_preflight_ignores_escapes_in_removed_code() -> None:
    result = obfuscate_modules(
        {
            Path("main.py"): (
                "def helper(value): return value\n"
                "def unused(): external(helper)\n"
                "__result__ = helper(value=4)\n"
            ),
        },
        ["f:helper*"],
        jobs=1,
    )
    assert result.renamed_parameters == 1
    assert "unused" not in result.source


@pytest.mark.parametrize(
    "entries",
    [[], ["f:helper"], ["f:helper*"], ["a:helper:value"], ["f:unused*"], ["f:Record:_get*"]],
)
def test_allowlist_preflight_preserves_exact_results_and_warnings(
    entries: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    originals = {
        Path("main.py"): (
            "class Record:\n"
            " def _get(self, value): return value + 1\n"
            "def helper(value): return Record()._get(value=value)\n"
            "def unused(value): return value\n"
            "__result__ = helper(value=4)\n"
        ),
    }
    actual = obfuscate_modules(originals, entries, jobs=1)
    actual_warnings = capsys.readouterr().err

    def skip_preflight(self: AllowlistRenamer, entries: list[str]) -> None:
        pass

    monkeypatch.setattr(AllowlistRenamer, "preflight", skip_preflight)
    expected = obfuscate_modules(originals, entries, jobs=1)
    assert actual == expected
    assert actual_warnings == capsys.readouterr().err


def test_combined_allowlist_preserves_original_output_snapshot() -> None:
    result = obfuscate_modules(
        {
            Path("main.py"): (
                "from dataclasses import dataclass\n"
                "@dataclass\n"
                "class Point:\n value: int\n"
                "class Record:\n"
                " def __init__(self, payload): self.payload = payload\n"
                " def read(self): return self.payload\n"
                " def _calculate(self, value): return value + 1\n"
                "def helper(value): return Record(Point(value)).read().value\n"
                "__result__ = helper(value=4)\n"
            ),
        },
        ["attr:Record:payload", "f:helper*", "c:Point", "f:Record:read"],
        jobs=1,
    )
    # Captured from the pre-optimization pipeline, including exact name allocation.
    assert result.source == (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass as A\n"
        "@A\n"
        "class e:value:int\n"
        "class g:\n"
        "\tdef __init__(a,b):a.c=b\n"
        "\tdef f(a):return a.c\n"
        "def d(a):return g(e(a)).f().value\n"
        "__result__=d(a=4)\n"
    )
    assert result.renames == {
        "f:helper*": "d", "c:Point": "e", "f:Record:read": "f", "attr:Record:payload": "c",
    }


@pytest.mark.parametrize(
    "source",
    [
        (
            "class Record:\n def _read(self, value): return value + 1\n"
            "def helper(value): return Record()._read(value=value)\n"
            "__result__ = helper(value=4)\n"
        ),
        (
            "class Record:\n def _read(self, value): return value\n"
            "record = Record()\ncallback = record._read\nprint(callback)\n"
        ),
        (
            "def helper(value, **kwargs): return value\n"
            "__result__ = helper(value=1, **{'extra': 2})\n"
        ),
    ],
)
def test_private_method_call_analysis_matches_fresh_analysis(source: str) -> None:
    def rename(*, reuse: bool) -> tuple[str, int, int, dict[str, int]]:
        tree = prepare_tree(source, "main.py")
        renamer = PrivateMethodRenamer({"__main__": tree})
        renamer.run()
        renamed_methods = renamer.renamed
        renamer.rename_parameters(reuse_calls=reuse)
        return ast.dump(tree), renamed_methods, renamer.renamed, dict(renamer.skipped)

    assert rename(reuse=True) == rename(reuse=False)


def test_allowlist_skips_unrelated_attribute_callable_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "class Record:\n"
        " def read(self, value): return value\n"
        "def other(value): return value.unrelated()\n"
        "result = Record().read(value=4)\n"
    )
    original = AllowlistRenamer.callable_targets

    def checked(self: AllowlistRenamer, node: ast.expr) -> set[ast.AST]:
        assert not (isinstance(node, ast.Attribute) and node.attr == "unrelated")
        return original(self, node)

    monkeypatch.setattr(AllowlistRenamer, "callable_targets", checked)
    assert "a:Record:read:value" in AllowlistRenamer(tree).apply(["a:Record:read:value"])


def test_unrelated_ambiguous_super_still_fails_closed() -> None:
    tree = ast.parse(
        "class A:\n"
        " def call(self): return super().foo()\n"
        "class Z:\n"
        " def foo(self): return 1\n"
        "class B(A, Z): pass\n"
        "def helper(value): return value\n"
        "helper(value=1)\n"
    )
    with pytest.raises(ValueError, match="Ambiguous super attribute"):
        AllowlistRenamer(tree).apply(["a:helper:value"])


def test_cleanup_precedes_minification(monkeypatch: pytest.MonkeyPatch) -> None:
    sources: list[str] = []

    def minify(source: str, **kwargs: object) -> str:
        sources.append(source)
        return source

    monkeypatch.setattr(python_minifier, "minify", minify)
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
    tree = prepare_tree(source, "fixture.py")
    result = minify_tree(tree, "fixture.py", rename_globals=rename_globals)
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
    tree = prepare_tree(source, "fixture.py")
    result = minify_tree(tree, "fixture.py", rename_globals=True)
    assert "long_global" not in result
    module = types.ModuleType("minified_globals_annotations_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(result, module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert capsys.readouterr().out == "[1, 4] Record callback 7\n"
    assert module.Record.__annotations__["value"] == "'Alias'"
    assert module.callback.__name__ == "callback"


def test_global_minification_keeps_dynamic_namespaces(capsys: pytest.CaptureFixture[str]) -> None:
    source = "long_global = 7\nprint(globals()['long_global'])\n"
    tree = prepare_tree(source, "fixture.py")
    result = minify_tree(tree, "fixture.py", rename_globals=True)
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
    tree = prepare_tree(source, "fixture.py")
    result = minify_tree(tree, "fixture.py", rename_globals=True)
    exec(result, {})  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert capsys.readouterr().out == "7\n"


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

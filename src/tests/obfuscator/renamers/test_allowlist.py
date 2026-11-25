from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("python_minifier")

from obfuscator.linker import flatten
from obfuscator.pipeline import minify_tree, obfuscate_modules, prepare_tree
from obfuscator.renamers.allowlist import AllowlistRenamer, read_allowlist
from obfuscator.renamers.base import ArgumentRenamer
from obfuscator.renamers.methods import PrivateMethodRenamer
from tests.support.obfuscator import allowlisted

__all__: list[str] = []


def test_allowlist_nested_bindings_and_keyword_aliases() -> None:
    source = """
CONSTANT = 3
def outer(value):
    def inner(argument, *items, **options):
        local = argument + CONSTANT
        return local, items, options
    alias = inner
    return alias(**{'argument': value}, extra=7)
__result__ = outer(value=4)
"""
    result, names = allowlisted(
        source, ["v:CONSTANT", "f:outer:inner", "a:outer:inner:argument", "v:outer:inner:local"]
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (7, (), {"extra": 7})
    assert len(names) == 4
    assert "def outer(value)" in result
    assert "*items,**options" in result


def test_allowlist_mixin_calls_and_inherited_class_variables() -> None:
    source = """
class LeaderboardMixin:
    constant = 3
    def _cb_leaderboard_type(self, chat_id):
        return chat_id + self.constant
class RoutingMixin:
    def route(self):
        return self._cb_leaderboard_type(chat_id=4)
class Admin(RoutingMixin, LeaderboardMixin): pass
admin = Admin()
__result__ = admin.route(), Admin.constant
"""
    result, names = allowlisted(
        source,
        [
            "f:LeaderboardMixin:_cb_leaderboard_type",
            "a:LeaderboardMixin:_cb_leaderboard_type:chat_id",
            "v:LeaderboardMixin:constant",
        ],
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (7, 3)
    assert len(names) == 3
    assert "_cb_leaderboard_type" not in result
    assert "chat_id" not in result


def test_allowlist_class_renames_definition_and_lexical_references_only() -> None:
    source = """
class Helper:
    class Nested: pass
    def method(self, value): return value
alias = Helper
class Child(Helper): pass
__result__ = Helper.__name__, alias is Helper, Child().method(value=7), Helper.Nested.__name__
"""
    result, renames = allowlisted(source, ["c:Helper"])
    assert set(renames) == {"c:Helper"}
    assert "class Helper" not in result
    assert "def method(self,value)" in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (renames["c:Helper"], True, 7, "Nested")


@pytest.mark.parametrize("collision", ["annotation", "local", "builtin"])
def test_allowlist_class_renames_linker_class_slots(collision: str) -> None:
    name: str = "len" if collision == "builtin" else "Helper"
    sources: dict[Path, str] = {
        Path("worker.py"): f"class {name}:\n def value(self): return 7\n",
        Path("facade.py"): f"from worker import {name}\n",
        Path("main.py"): f"from facade import {name} as Selected\n"
        "alias = Selected\n"
        "__result__ = Selected.__name__, alias is Selected, Selected().value()\n",
    }
    if collision == "annotation":
        # TYPE_CHECKING-only imports leave unresolved runtime field annotations.
        sources = {Path("data.py"): "class Record:\n item: Helper\n", **sources}
    elif collision == "local":
        sources[Path("main.py")] += (
            "import worker\n"
            "def invoke(Helper): return worker.Helper().value(), Helper\n"
            "assert invoke('local') == (7, 'local')\n"
        )
    else:
        sources[Path("main.py")] += "assert len([1, 2]) == 2\n"
    entry: str = f"c:{name}"
    result = obfuscate_modules(sources, [entry])
    assert set(result.renames) == {entry}
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (result.renames[entry], True, 7)


@pytest.mark.parametrize("rebind", ["Helper = object", "del Helper"])
def test_allowlist_class_slots_still_reject_real_rebindings(rebind: str) -> None:
    sources: dict[Path, str] = {
        Path("data.py"): "class Record:\n item: Helper\n",
        Path("worker.py"): f"class Helper: pass\n{rebind}\n",
    }
    with pytest.raises(ValueError, match="ambiguous class binding"):
        obfuscate_modules(sources, ["c:Helper"])


def test_allowlist_nested_classes_rewrite_inherited_attributes_and_local_references() -> None:
    source = """
class Outer:
    class Inner: pass
    def construct(self): return self.Inner()
class Child(Outer): pass
def factory():
    class Local: pass
    return Local()
__result__ = Outer.Inner.__name__, Child.Inner.__name__, Child().construct().__class__.__name__, factory().__class__.__name__
"""
    result, renames = allowlisted(source, ["c:Outer", "c:Outer:Inner", "c:factory:Local"])
    assert set(renames) == {"c:Outer", "c:Outer:Inner", "c:factory:Local"}
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (
        renames["c:Outer:Inner"],
        renames["c:Outer:Inner"],
        renames["c:Outer:Inner"],
        renames["c:factory:Local"],
    )


@pytest.mark.parametrize(
    ("source", "entries", "error"),
    [
        ("def run(value): return value", ["v:run"], "incorrect type"),
        ("def run(): pass", ["c:run"], "incorrect type"),
        ("class Helper: pass", ["f:Helper"], "incorrect type"),
        ("class Helper: pass", ["v:Helper"], "incorrect type"),
        ("class Helper: pass\nclass Helper: pass", ["c:Helper"], "ambiguous"),
        ("class Helper: pass\nHelper = object", ["c:Helper"], "ambiguous class binding"),
        ("class Helper: pass\ndel Helper", ["c:Helper"], "ambiguous class binding"),
        ("class Helper: pass", ["c:Missing"], "target not found"),
        ("value = 3", ["f:missing"], "target not found"),
        ("def run(*items): pass", ["a:run:items"], r"\*args/\*\*kwargs"),
        ("def run(value): return value\noptions = {}\nrun(**options)", ["a:run:value"], "Dynamic"),
        ("def run(value): return value\ncallbacks = [run]", ["a:run:value"], "Escaping"),
        (
            "class C:\n def method(self): pass\ndef run(obj): obj.method()",
            ["f:C:method"],
            "Unresolved receiver",
        ),
        ("def run(): pass\ndef run(): pass", ["f:run"], "ambiguous"),
        (
            "class Base:\n def method(self): pass\n def run(self): self.method()\nclass Child(Base):\n def method(self): pass",
            ["f:Base:method"],
            "Ambiguous polymorphic",
        ),
    ],
)
def test_allowlist_rejects_unsafe_or_incorrect_targets(
    source: str, entries: list[str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        allowlisted(source, entries)


@pytest.mark.parametrize(
    ("entry", "suggestion"),
    [
        ("f:method", "f:Helper:method"),
        ("f:method*", "f:Helper:method*"),
        ("f:method:inner", "f:Helper:method:inner"),
        ("a:method:value", "a:Helper:method:value"),
        ("v:method:local", "v:Helper:method:local"),
        ("c:Nested", "c:Helper:Nested"),
    ],
)
def test_allowlist_missing_scope_suggests_full_lexical_path(entry: str, suggestion: str) -> None:
    source = """
class Helper:
    class Nested: pass
    def method(self, value):
        local = value
        def inner(): return local
        return inner()
"""
    with pytest.raises(ValueError, match="target not found") as error:
        allowlisted(source, [entry])
    assert f"use the full lexical path: {suggestion!r}" in str(error.value)


def test_allowlist_missing_scope_lists_all_matching_paths() -> None:
    source = """
class Second:
    def method(self): pass
class First:
    def method(self): pass
class Unrelated:
    method = 3
"""
    with pytest.raises(ValueError, match="target not found") as error:
        allowlisted(source, ["f:method*"])
    assert str(error.value).endswith(
        "use the full lexical path: 'f:First:method*', 'f:Second:method*'"
    )


def test_allowlist_missing_target_does_not_suggest_wrong_types() -> None:
    with pytest.raises(ValueError, match="target not found") as error:
        allowlisted("class Helper:\n    method = 3", ["f:method*"])
    assert "use the full lexical path" not in str(error.value)


def test_allowlist_input(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.json"
    path.touch()
    assert read_allowlist(str(path)) == []
    path.write_text('["f:run"]', encoding="utf-8")
    assert read_allowlist(str(path)) == ["f:run"]
    assert read_allowlist('["v:VALUE", "a:run:argument", "c:Helper", "c:Outer:Inner"]') == [
        "v:VALUE",
        "a:run:argument",
        "c:Helper",
        "c:Outer:Inner",
    ]
    for invalid in ("{}", "[1]", '["z:run"]', '["a:run"]', '["f:run", "f:run"]'):
        with pytest.raises(ValueError, match=r"Allowlist|allowlist"):
            read_allowlist(invalid)


def test_allowlist_function_star_expands_parameters_and_rewrites_keyword_aliases() -> None:
    source = """
def helper(value, /, *items, scale=2, **options):
    return value * scale, items, options
alias = helper
__result__ = alias(value=3, scale=4, extra=7)
"""
    entries = read_allowlist('["f:helper*", "a:helper:value"]')
    result, renames = allowlisted(source, entries)
    assert set(renames) == {"f:helper*", "a:helper:value", "a:helper:scale"}
    function = next(node for node in ast.parse(result).body if isinstance(node, ast.FunctionDef))
    assert function.args.vararg is not None
    assert function.args.vararg.arg == "items"
    assert function.args.kwarg is not None
    assert function.args.kwarg.arg == "options"
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (12, (), {"extra": 7})


@pytest.mark.parametrize("descriptor", ["", "@classmethod", "@staticmethod"])
def test_allowlist_method_star_includes_receiver_when_present(descriptor: str) -> None:
    receiver = "" if descriptor == "@staticmethod" else "self, "
    source = f"""
class Helper:
    {descriptor}
    def method({receiver}value, *items, scale=2, **options):
        return value * scale, items, options
__result__ = Helper().method(value=3, scale=4, extra=7)
"""
    result, renames = allowlisted(source, ["f:Helper:method*"])
    assert "a:Helper:method:value" in renames
    assert "a:Helper:method:scale" in renames
    assert ("a:Helper:method:self" in renames) == bool(receiver)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (12, (), {"extra": 7})


@pytest.mark.parametrize("method", ["__init__", "__call__", "__dunder__"])
def test_allowlist_dunder_name_is_ignored_with_warning(
    capsys: pytest.CaptureFixture[str], method: str,
) -> None:
    source = f"class Helper:\n def {method}(self, value): return value\n"
    entry = f"f:Helper:{method}"
    result, renames = allowlisted(source, [entry])
    assert renames == {}
    assert f"def {method}(self,value)" in result
    warning = capsys.readouterr().err
    assert f"Warning: skipping allowlist {entry!r}" in warning
    assert "dunder function names must be preserved" in warning


@pytest.mark.parametrize("method", ["__init__", "__call__", "__dunder__"])
def test_allowlist_dunder_star_only_renames_parameters(
    capsys: pytest.CaptureFixture[str], method: str,
) -> None:
    invocation = (
        "Helper(value=3, **{'scale': 4}, extra=7).value"
        if method == "__init__"
        else f"Helper().{method}(value=3, **{{'scale': 4}}, extra=7)"
    )
    source = f"""
class Helper:
    def {method}(self, /, value, *items, scale=2, **options):
        self.value = value * scale, items, options
        {'' if method == '__init__' else 'return self.value'}
__result__ = {invocation}
"""
    result, renames = allowlisted(source, [f"f:Helper:{method}*"])
    assert set(renames) == {
        f"a:Helper:{method}:{name}" for name in ("self", "value", "scale")
    }
    assert f"def {method}(" in result
    assert "*items" in result
    assert "**options" in result
    assert capsys.readouterr().err == ""
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (12, (), {"extra": 7})


def test_allowlist_dunder_star_preserves_keyword_safety_checks() -> None:
    with pytest.raises(ValueError, match="Dynamic"):
        allowlisted(
            "class Helper:\n def __init__(self, value): pass\n"
            "options = {'value': 3}\nHelper(**options)",
            ["f:Helper:__init__*"],
        )


def test_allowlist_star_is_not_recursive() -> None:
    source = """
def outer(value):
    def inner(argument): return argument + value
    return inner(argument=3)
__result__ = outer(value=4)
"""
    result, renames = allowlisted(source, ["f:outer*"])
    assert set(renames) == {"f:outer*", "a:outer:value"}
    assert "def inner(argument)" in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7
    _, nested_renames = allowlisted(source, ["f:outer:inner*"])
    assert set(nested_renames) == {"f:outer:inner*", "a:outer:inner:argument"}


@pytest.mark.parametrize(
    "entry",
    [
        "a:helper:value*",
        "v:VALUE*",
        "f:helper**",
        "f:*",
        "f:helper:*",
        "f:hel*per",
        "c:Helper*",
        "c:Outer:Inner*",
        "c:*",
        "c:Hel*per",
    ],
)
def test_allowlist_rejects_invalid_star_syntax(entry: str) -> None:
    with pytest.raises(ValueError, match="Invalid allowlist"):
        read_allowlist(f'["{entry}"]')


def test_allowlist_star_preserves_keyword_safety_checks() -> None:
    with pytest.raises(ValueError, match="Dynamic"):
        allowlisted(
            "def helper(value): return value\noptions = {'value': 3}\nhelper(**options)",
            ["f:helper*"],
        )


def test_allowlist_callback_name_can_be_renamed_without_selecting_parameters() -> None:
    source = """
import json
def reject_nonfinite_constant(value): return value.lower()
__result__ = json.loads("NaN", parse_constant=reject_nonfinite_constant)
"""
    result, renames = allowlisted(source, ["f:reject_nonfinite_constant"])
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == "nan"
    assert set(renames) == {"f:reject_nonfinite_constant"}
    with pytest.raises(ValueError, match="Escaping callable"):
        allowlisted(source, ["f:reject_nonfinite_constant*"])


def test_allowlist_parameter_only_rejects_unknown_method_receivers() -> None:
    with pytest.raises(ValueError, match="Unresolved receiver"):
        allowlisted(
            "class C:\n def method(self, value): return value\ndef run(obj): return obj.method(value=3)",
            ["a:C:method:value"],
        )


_SAME_NAMED_METHODS: str = """
from typing import Protocol

class Snapshot(Protocol):
    def view(self) -> int: ...

class Config:
    def view(self) -> int: return 1

class AsyncConfig:
    def view(self) -> int: return 2

def read_snapshot(cfg: Snapshot) -> int:
    return cfg.view()
"""


@pytest.mark.parametrize("also_rename_async", [False, True])
def test_allowlist_resolves_same_named_methods_through_distinct_callers(also_rename_async: bool) -> None:
    source = _SAME_NAMED_METHODS + """
def read_config(cfg: Snapshot) -> int:
    return cfg.view()

__result__ = read_config(Config()), read_snapshot(AsyncConfig())
"""
    entries = ["f:Config:view"]
    if also_rename_async:
        entries.append("f:AsyncConfig:view")
    result = obfuscate_modules({Path("main.py"): source}, entries, jobs=1)
    assert set(result.renames) == set(entries)
    assert len(set(result.renames.values())) == len(entries)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (1, 2)
    assert sum(
        isinstance(node, ast.FunctionDef) and node.name == "view"
        for node in ast.walk(ast.parse(result.source))
    ) == (0 if also_rename_async else 1)


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ("read_snapshot(Config())", "Ambiguous polymorphic attribute"),
        ("opaque(read_snapshot)", "Unresolved receiver"),
        ("read_snapshot(opaque())", "Unresolved receiver"),
    ],
)
def test_allowlist_rejects_mixed_or_unknown_method_receiver_flow(extra: str, error: str) -> None:
    source = _SAME_NAMED_METHODS + f"\nread_snapshot(AsyncConfig())\n{extra}\n"
    with pytest.raises(ValueError, match=error):
        obfuscate_modules({Path("main.py"): source}, ["f:Config:view"], jobs=1)


@pytest.mark.parametrize("factory", ["AsyncConfig()", "opaque()"])
def test_allowlist_traces_protocol_receivers_through_dataclass_fields(factory: str) -> None:
    source = _SAME_NAMED_METHODS + f"""
from dataclasses import dataclass

@dataclass(frozen=True, slots=True, kw_only=True)
class Resources:
    lang_cfg: Snapshot

class Subscription:
    def __init__(self, lang_cfg: Snapshot):
        self.res = Resources(lang_cfg=lang_cfg)

class BaseRoutes:
    def __init__(self, sub):
        self.sub, self.enabled = sub, True

class Routes(BaseRoutes):
    def __init__(self, sub):
        super().__init__(sub)

def handler(extension):
    routes: Routes | None = extension.get('routes')
    if routes is not None:
        return read_snapshot(routes.sub.res.lang_cfg)

routes = Routes(Subscription({factory}))
__result__ = Config().view(), handler({{'routes': routes}})
"""
    originals = {Path("main.py"): source}
    if factory == "opaque()":
        with pytest.raises(ValueError, match="Unresolved receiver"):
            obfuscate_modules(originals, ["f:Config:view"], jobs=1)
        return
    result = obfuscate_modules(originals, ["f:Config:view"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (1, 2)


@pytest.mark.parametrize("changes_receiver", [False, True])
def test_allowlist_traces_verified_local_wrappers(changes_receiver: bool) -> None:
    forwarded = "function(Config(), **kwargs)" if changes_receiver else "function(*args, **kwargs)"
    source = _SAME_NAMED_METHODS + f"""
from functools import wraps
from inspect import signature

def decorate(function):
    sig = signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        sig.bind(*args, **kwargs)
        return {forwarded}
    return wrapped

def compose(function):
    delegate = decorate(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        return delegate(*args, **kwargs)
    return wrapped

class Service:
    def __init__(self, cfg: Snapshot):
        self.cfg = cfg

    @compose
    def read(self):
        return self.cfg.view()

__result__ = Config().view(), Service(AsyncConfig()).read(), read_snapshot(AsyncConfig())
"""
    originals = {Path("main.py"): source}
    if changes_receiver:
        with pytest.raises(ValueError, match="Unresolved receiver"):
            obfuscate_modules(originals, ["f:Config:view"], jobs=1)
        return
    result = obfuscate_modules(originals, ["f:Config:view"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (1, 2, 2)


def test_allowlist_resolves_annotated_chained_receivers() -> None:
    source = """
class Database:
    def query(self, long_parameter):
        return long_parameter * 2
class Resources:
    db: Database
def run(res: Resources):
    return res.db.query(long_parameter=3)
"""
    result, renames = allowlisted(source, ["a:Database:query:long_parameter"])
    assert set(renames) == {"a:Database:query:long_parameter"}
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    resources = namespace["Resources"]()
    resources.db = namespace["Database"]()
    assert namespace["run"](resources) == 6


@pytest.mark.parametrize(
    ("source", "entry", "message"),
    [
        (
            "def run(value): return value\nrun = lambda value: value\nrun(value=3)",
            "a:run:value",
            "reassigned callable",
        ),
        (
            "class C:\n def method(self, value): return value\nC.method = lambda self, value: value\nC().method(value=3)",
            "a:C:method:value",
            "Reassigned method",
        ),
    ],
)
def test_allowlist_rejects_reassigned_parameter_apis(source: str, entry: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        allowlisted(source, [entry])


@pytest.mark.parametrize(
    ("source", "entry"),
    [("def run(value): return value", "f:run"), ("class Helper: pass", "c:Helper")],
)
def test_allowlist_ambiguity_survives_linker_collision_renames(source: str, entry: str) -> None:
    trees = {name: prepare_tree(source, f"{name}.py") for name in ("first", "second")}
    tree = flatten(trees, rename_classes=False)
    with pytest.raises(ValueError, match="ambiguous"):
        AllowlistRenamer(tree).apply([entry])


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mode", ["together", "separate", "automatic"])
def test_allowlist_nested_formatter_survives_enclosing_method_rename(
    capsys: pytest.CaptureFixture[str], reverse: bool, mode: str
) -> None:
    source = """
import logging as F
class Colors:
    RESET = '</>'
class Logger:
    COLORS = {'INFO': '<green>'}
    def _make_formatter(self):
        A = self
        class Fmt(F.Formatter):
            def format(self, record):
                B = record.levelname
                color = A.COLORS.get(record.levelname, Colors.RESET)
                record.levelname = f'{color}{record.levelname}{Colors.RESET}'
                C = super().format(record)
                record.levelname = B
                return C
        return Fmt(fmt='%(levelname)s %(message)s')
logger = Logger()
formatter = logger._make_formatter()
record = F.LogRecord('fixture', F.INFO, '', 0, 'hello', (), None)
__result__ = formatter.format(record), record.levelname, formatter.__class__.__name__
"""
    tree = flatten({"main": prepare_tree(source, "main.py")})
    entries = ["f:Logger:_make_formatter", "c:Logger:_make_formatter:Fmt"]
    if reverse:
        entries.reverse()
    if mode == "automatic":
        methods = PrivateMethodRenamer({"__main__": tree})
        methods.run()
        assert methods.renamed == 1
        ArgumentRenamer({"__main__": tree}).run()
    if mode == "separate":
        renames: dict[str, str] = {}
        for entry in entries:
            renames.update(AllowlistRenamer(tree).apply([entry]))
    else:
        renames = AllowlistRenamer(tree).apply(entries)
    expected = {"c:Logger:_make_formatter:Fmt"} if mode == "automatic" else set(entries)
    assert set(renames) == expected
    warnings = capsys.readouterr().err
    assert ("Warning: skipping allowlist 'f:Logger:_make_formatter'" in warnings) == (
        mode == "automatic"
    )
    assert "skipping allowlist 'c:" not in warnings
    result = minify_tree(tree, "fixture.py", rename_globals=True)
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (
        "<green>INFO</> hello",
        "INFO",
        renames["c:Logger:_make_formatter:Fmt"],
    )


def test_allowlist_selects_original_scopes_already_renamed_by_linker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    trees = {
        "worker": prepare_tree(
            "def len(value):\n local = value + 1\n return local\n",
            "worker.py",
        ),
        "main": prepare_tree(
            "import worker\ndef helper(value): return value\n"
            "__result__ = worker.len(value=4), len([1, 2]), helper(value=3)\n",
            "main.py",
        ),
    }
    tree = flatten(trees, rename_classes=False)
    renames = AllowlistRenamer(tree).apply(["f:len", "a:len:value", "v:len:local", "f:helper"])
    assert set(renames) == {"a:len:value", "v:len:local", "f:helper"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("Warning: skipping allowlist") == 1
    assert "f:len" in captured.err
    assert "a:len:value" not in captured.err
    assert "v:len:local" not in captured.err
    assert "already renamed" in captured.err
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (5, 2, 3)


def test_allowlist_skips_previously_renamed_variables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    trees = {
        "worker": prepare_tree("len = 7", "worker.py"),
        "main": prepare_tree("__result__ = len([1, 2])", "main.py"),
    }
    tree = flatten(trees, rename_classes=False)
    assert AllowlistRenamer(tree).apply(["v:len"]) == {}
    assert "Warning: skipping allowlist 'v:len'" in capsys.readouterr().err
    with pytest.raises(ValueError, match="incorrect type"):
        AllowlistRenamer(tree).apply(["f:len"])


def test_allowlist_repeated_application_skips_all_already_renamed_targets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tree = prepare_tree(
        "CONSTANT = 2\ndef helper(value): return value + CONSTANT\n__result__ = helper(value=3)",
        "fixture.py",
    )
    entries = ["f:helper", "a:helper:value", "v:CONSTANT"]
    assert len(AllowlistRenamer(tree).apply(entries)) == 3
    assert capsys.readouterr().err == ""
    assert AllowlistRenamer(tree).apply(entries) == {}
    assert capsys.readouterr().err.count("Warning: skipping allowlist") == 3
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 5


def test_allowlist_does_not_guess_mangling_from_short_names() -> None:
    result, renames = allowlisted("def a(): return 3\n__result__ = a()", ["f:a"])
    assert set(renames) == {"f:a"}
    assert "def a(" not in result
    with pytest.raises(ValueError, match="target not found"):
        allowlisted("def a(): return 3", ["f:helper"])


@pytest.mark.parametrize("class_slot", [False, True])
def test_allowlist_class_repeated_application_skips_previous_rename(
    capsys: pytest.CaptureFixture[str],
    class_slot: bool,
) -> None:
    tree = prepare_tree("class Helper: pass", "fixture.py")
    if class_slot:
        tree = flatten(
            {"data": prepare_tree("class Record:\n item: Helper", "data.py"), "worker": tree}
        )
    assert set(AllowlistRenamer(tree).apply(["c:Helper"])) == {"c:Helper"}
    assert AllowlistRenamer(tree).apply(["c:Helper"]) == {}
    assert "Warning: skipping allowlist 'c:Helper'" in capsys.readouterr().err


def test_allowlist_skips_automatically_renamed_private_class_slots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tree = flatten({"worker": prepare_tree("class Outer:\n class _Private: pass", "worker.py")})
    assert AllowlistRenamer(tree).apply(["c:Outer:_Private"]) == {}
    assert "Warning: skipping allowlist 'c:Outer:_Private'" in capsys.readouterr().err
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["Outer"]._Private.__name__.startswith("_g")


@pytest.mark.parametrize(
    ("imports", "annotation", "expected"),
    [
        ("from typing import Literal", 'dict[str, Literal["ok", "failed"]]', "dict[str, str]"),
        ("from typing import Literal as L", 'L["ok"]', "str"),
        ("import typing as t", 't.Literal["ok"]', "str"),
        ("from typing import Annotated", 'Annotated[str, "ok"]', "Annotated[str, 'ok']"),
        ("import typing as t", 't.Annotated[str, "ok"]', "t.Annotated[str, 'ok']"),
    ],
)
def test_allowlist_does_not_treat_annotation_values_as_forward_references(
    imports: str, annotation: str, expected: str
) -> None:
    source = f"""
from __future__ import annotations
from dataclasses import dataclass
{imports}
@dataclass
class Record:
    status: {annotation}
def ok(): return "ok"
__result__ = Record(ok()).status
__record_type__ = Record
"""
    result, renames = allowlisted(source, ["f:ok"])
    assert "f:ok" in renames
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == "ok"
    assert namespace["Record"].__annotations__["status"] == expected


@pytest.mark.parametrize(
    "annotation",
    ['"Helper"', 'list["Helper"]', 'Annotated["Helper", "tag"]', 'Literal["Helper"]'],
)
def test_allowlist_still_rejects_quoted_type_references(annotation: str) -> None:
    source = f"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Annotated
class Helper: pass
class Literal: pass
@dataclass
class Record:
    value: {annotation}
"""
    with pytest.raises(ValueError, match="quoted runtime annotation: c:Helper"):
        allowlisted(source, ["c:Helper"])


def test_allowlist_preserves_dacite_field_types(monkeypatch: pytest.MonkeyPatch) -> None:
    source = """
from dataclasses import dataclass
from dacite import from_dict
@dataclass
class Client:
    value: int
@dataclass
class Response:
    clients: list[Client]
def decode(payload): return from_dict(Response, payload)
__result__ = decode(payload={'clients': [{'value': 3}]})
"""
    result, _ = allowlisted(source, ["f:decode", "a:decode:payload"])
    module = types.ModuleType("allowlisted_dacite_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(result, module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert isinstance(module.__result__.clients[0], module.Client)
    assert module.__result__.clients[0].value == 3


def test_allowlist_resolves_nested_dataclass_chain_parameters() -> None:
    source = """
from dataclasses import dataclass
@dataclass
class Database:
    def query(self, long_parameter):
        return long_parameter * 2
@dataclass
class Pool:
    db: Database
@dataclass
class Config:
    pool: Pool
class Service:
    def __init__(self, config: Config):
        self.config = config
    def run(self):
        return self.config.pool.db.query(long_parameter=4)
result = Service(Config(Pool(Database()))).run()
"""
    result, renames = allowlisted(source, ["a:Database:query:long_parameter"])
    assert set(renames) == {"a:Database:query:long_parameter"}
    assert "long_parameter=" not in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 8


def test_allowlist_method_copy_ignores_rebound_typed_dict_union() -> None:
    source = """
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from hypercorn.typing import Scope as RequestScope

class Config:
    def copy(self):
        return 42

class Middleware:
    def run(self, scope: RequestScope):
        scope = scope.copy()
        backup = scope.copy()
        backup['type'] = 'websocket'
        return scope, backup

__result__ = Config().copy(), Middleware().run({'type': 'http'})
"""
    result, renames = allowlisted(source, ["f:Config:copy"])
    assert f".{renames['f:Config:copy']}()" in result
    assert result.count(".copy()") == 2
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (42, ({"type": "http"}, {"type": "websocket"}))


def test_allowlist_method_copy_rejects_conditional_rebinding_to_unknown() -> None:
    source = """
from __future__ import annotations
from hypercorn.typing import Scope

class Config:
    def copy(self):
        return 42

def middleware(scope: Scope, opaque, flag):
    if flag:
        scope = opaque()
    return scope.copy()

__result__ = Config().copy()
"""
    with pytest.raises(ValueError, match=r"Unresolved receiver.*scope\.copy"):
        allowlisted(source, ["f:Config:copy"])


def test_allowlist_method_copy_rejects_incomplete_union() -> None:
    source = """
from __future__ import annotations
from typing import Any
from hypercorn.typing import Scope

class Config:
    def copy(self):
        return 42

def middleware(scope: Scope | Any):
    return scope.copy()

__result__ = Config().copy()
"""
    with pytest.raises(ValueError, match=r"Unresolved receiver.*scope\.copy"):
        allowlisted(source, ["f:Config:copy"])

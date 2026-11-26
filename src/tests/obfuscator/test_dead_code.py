from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("python_minifier")

from obfuscator.dead_code import eliminate_dead_code
from obfuscator.pipeline import obfuscate_modules, prepare_tree
from obfuscator.renamers.allowlist import AllowlistRenamer

__all__: list[str] = []


def prune(source: str) -> ast.Module:
    tree = prepare_tree(source, "fixture.py")
    eliminate_dead_code(tree)
    compile(tree, "fixture.py", "exec")
    return tree


def names(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_removes_dead_chains_cycles_and_nested_functions() -> None:
    tree = prune("""
def leaf(): return 10
def dead(): return leaf()
def recursive(): return recursive()
def first(): return second()
def second(): return first()
def live():
    def unused(): return leaf()
    def used(): return 42
    return used()
__result__ = live()
""")
    assert names(tree) == {"live", "used"}
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 42


def test_removes_closed_methods_and_async_functions() -> None:
    tree = prune("""
class Worker:
    def live(self): return 42
    def dead(self): return self.helper()
    def helper(self): return 1
    async def unused(self): return 2
    def __len__(self): return 3
worker = Worker()
__result__ = worker.live()
async def unused_function(): return 0
""")
    assert names(tree) == {"live", "__len__"}


@pytest.mark.parametrize(
    "use",
    [
        "external(worker)",
        "getattr(worker, key)",
        "vars(worker)",
        "worker.__dict__",
        "external(Worker)",
    ],
)
def test_preserves_methods_of_escaping_or_reflected_receivers(use: str) -> None:
    assert "callback" in names(
        prune(f"""
class Worker:
    def callback(self): return 1
worker = Worker()
{use}
""")
    )


@pytest.mark.parametrize(
    "source",
    [
        "def callback(): return 1\nexternal(callback)",
        "def callback(): return 1\nalias = callback\nexternal(alias)",
        "def callback(): return 1\nglobals()[key]()",
        "def callback(): return 1\neval(expression)",
        "def callback(): return 1\nexec(expression)",
        "def callback(): return 1\nimport sys\nsys._getframe()",
        "@external\ndef callback(): return 1",
        "def callback(value=external()): return 1",
        "class Worker(External):\n    def callback(self): return 1",
        "@external\nclass Worker:\n    def callback(self): return 1",
        "class Worker:\n    @property\n    def callback(self): return 1",
    ],
)
def test_preserves_uncertain_uses_and_definition_effects(source: str) -> None:
    assert "callback" in names(prune(source))


def test_defaults_execute_even_when_function_is_not_called() -> None:
    tree = prune("""
def default(): return 42
def unused(value=default()): return value
""")
    assert names(tree) == {"default", "unused"}


def test_empty_suites_remain_valid() -> None:
    tree = prune("""
class Empty:
    def unused(self): return 1
if True:
    def dead(): return 2
try:
    def dead_try(): return 3
except Exception:
    def dead_except(): return 4
try:
    def dead_body(): return 5
finally:
    def dead_finally(): return 6
""")
    assert names(tree) == set()


def test_nonlocal_declarations_keep_their_enclosing_binding() -> None:
    tree = prune("""
def outer():
    def binding(): return 1
    def inner():
        nonlocal binding
        return 42
    return inner()
__result__ = outer()
""")
    assert names(tree) == {"outer", "inner", "binding"}


def test_allowlist_warns_for_dead_definitions_and_their_parameters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = obfuscate_modules(
        {
            Path("main.py"): """
def dead(value):
    def nested(): return value
    return nested()
class Worker:
    def unused(self): return 1
__result__ = 42
"""
        },
        ["f:main.py:dead*", "a:dead:value", "f:dead:nested", "f:Worker:unused"],
        jobs=1,
    )
    assert result.renames == {}
    warnings = capsys.readouterr().err
    assert warnings.count("dead code removed") == 4
    assert "def " not in result.source


def test_cross_module_references_are_live() -> None:
    result = obfuscate_modules(
        {
            Path("worker.py"): "def live(): return 42\ndef dead(): return 0\n",
            Path("main.py"): "from worker import live\n__result__ = live()\n",
        },
        [],
        jobs=1,
    )
    assert "dead" not in result.source
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 42


def test_shadowed_functions_and_rebound_definitions() -> None:
    tree = prune("""
def unused(): return 0
def live():
    def unused(): return 42
    return unused()
def rebound(): return 1
rebound = external
__result__ = live()
""")
    assert names(tree) == {"live", "unused", "rebound"}
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name == "unused" for node in tree.body
    )


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("f:missing", "target not found"),
        ("c:dead", "incorrect type"),
        ("f:helper", "ambiguous target"),
    ],
)
def test_dead_metadata_preserves_allowlist_validation(entry: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        obfuscate_modules(
            {
                Path("first.py"): "def dead(): return 0\ndef helper(): return 1\n",
                Path("second.py"): "def helper(): return 2\n__result__ = helper()\n",
            },
            [entry],
            jobs=1,
        )


def test_qualified_dead_selector_does_not_hide_live_collision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = obfuscate_modules(
        {
            Path("first.py"): "def helper(): return 1\n",
            Path("second.py"): "def helper(): return 2\n__result__ = helper()\n",
        },
        ["f:first.py:helper", "f:second.py:helper"],
        jobs=1,
    )
    assert set(result.renames) == {"f:second.py:helper"}
    assert "dead code removed" in capsys.readouterr().err


def test_dead_local_with_multiple_assignments_warns_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = obfuscate_modules(
        {
            Path("main.py"): """
def dead():
    value = 1
    value = 2
    return value
"""
        },
        ["v:dead:value"],
        jobs=1,
    )
    assert not result.renames
    assert capsys.readouterr().err.count("dead code removed") == 1


def test_real_logger_removes_unused_context_manager_method(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[2]
    sources = {
        Path("loggers") / name: (root / "loggers" / name).read_text(encoding="utf-8")
        for name in ("logger.py", "colors.py", "common.py", "level.py")
    }
    sources[Path("main.py")] = (
        "from loggers.logger import Logger\n"
        "__logger__ = Logger('dead_code_fixture')\n"
        "__logger__.info('still working')\n"
    )
    result = obfuscate_modules(sources, ["f:Logger:span*"], jobs=1)
    assert "span" not in names(ast.parse(result.source))
    assert "dead code removed" in capsys.readouterr().err


def test_inherited_override_and_unknown_external_consumers_are_retained() -> None:
    tree = prune("""
import logging
class Worker(logging.Logger):
    def handle(self, record): return 42
    def unused(self): return 0
worker = Worker('fixture')
external.consume(worker)
""")
    assert {"handle", "unused"} <= names(tree)


def test_inspected_external_inheritance_can_drop_new_methods() -> None:
    tree = prune("""
import logging
from contextlib import contextmanager
class Worker(logging.Logger):
    def handle(self, record): return 42
    @contextmanager
    def unused(self): yield 0
worker = Worker('fixture')
worker.info('hello')
""")
    assert "handle" in names(tree)
    assert "unused" not in names(tree)


def test_inherited_family_cache_keeps_per_method_conflicts_and_receiver_escapes() -> None:
    tree = prune("""
import logging
class Closed(logging.Logger):
    def handle(self, record): return 42
    def unused_first(self): return 0
    def unused_second(self): return 1
class Escaping(logging.Logger):
    def callback_first(self): return 0
    def callback_second(self): return 1
closed = Closed('closed')
closed.info('hello')
escaping = Escaping('escaping')
external(escaping)
""")
    assert names(tree) == {"handle", "callback_first", "callback_second"}


def test_external_container_checks_do_not_solve_attribute_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_flow(self: AllowlistRenamer) -> object:
        pytest.fail("Dead-code annotation lookup built the whole-program flow graph")

    monkeypatch.setattr(AllowlistRenamer, "attribute_flow", property(unexpected_flow))
    tree = prune("""
import logging
from weakref import WeakSet
class Worker(logging.Logger):
    def unused_first(self): return 0
    def unused_second(self): return 1
registry = WeakSet()
worker = Worker('fixture')
registry.add(worker)
worker.info('hello')
""")
    assert not names(tree)


@pytest.mark.parametrize(
    "use",
    [
        "external(worker)",
        "external.consume(worker)",
        "getattr(worker, key)",
        "vars(worker)",
        "worker.__dict__",
        "len(worker)",
    ],
)
def test_inherited_methods_keep_opaque_consumers_and_dynamic_lookups(use: str) -> None:
    tree = prune(f"""
import logging
from external import len
class Worker(logging.Logger):
    def unused(self): return 0
worker = Worker('fixture')
{use}
""")
    assert "unused" in names(tree)


def test_unused_builtin_method_descriptors_and_context_wrappers_are_removed() -> None:
    tree = prune("""
from contextlib import contextmanager, asynccontextmanager
class Worker:
    @staticmethod
    def unused_static(): return 0
    @classmethod
    def unused_class(cls): return 0
    @contextmanager
    def unused_context(self): yield 0
    @asynccontextmanager
    async def unused_async_context(self): yield 0
worker = Worker()
""")
    assert not names(tree)

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from obfuscator.linker import flatten
from obfuscator.pipeline import minify_tree, obfuscate_modules, prepare_tree
from obfuscator.renamers.allowlist import AllowlistRenamer, read_allowlist

__all__: list[str] = []


@pytest.mark.parametrize("kind", ["c", "f", "a", "v"])
def test_file_selector_parsing(kind: str) -> None:
    entry = f"{kind}:one/two/three.py:Outer:method:wrapper:inner:argument"
    assert read_allowlist(json.dumps([entry])) == [entry]


@pytest.mark.parametrize(
    "entry",
    [
        "f:db.backup:run", "f:db/backup:run", "f:/db/backup.py:run",
        "f:../backup.py:run", "f:db//backup.py:run", "f:db/./backup.py:run",
        "f:run:db/backup.py", "f:db/backup.py", "a:db/backup.py:run",
        "f:db/backup.py:run:other.py", "c:db/backup.py:Record*",
    ],
)
def test_invalid_file_selectors(entry: str) -> None:
    with pytest.raises(ValueError, match="Invalid allowlist"):
        read_allowlist(json.dumps([entry]))


def test_qualified_collision_functions_receive_short_names() -> None:
    entries = ["f:db/backup.py:do_backup", "f:config/backup.py:do_backup"]
    result = obfuscate_modules(
        {
            Path("db/backup.py"): "def do_backup(): return 1",
            Path("db/database.py"): "class BackupMixin: pass\nclass Database(BackupMixin): pass",
            Path("config/backup.py"): "def do_backup(): return 2",
            Path("main.py"): (
                "from db.backup import do_backup as db_backup\n"
                "from config.backup import do_backup as config_backup\n"
                "__result__ = db_backup(), config_backup()"
            ),
        },
        entries,
        jobs=1,
    )
    assert set(result.renames) == set(entries)
    assert len(set(result.renames.values())) == 2
    assert all(len(name) < 4 for name in result.renames.values())
    assert "_g" not in result.source
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (1, 2)


@pytest.mark.parametrize("kind", ["c", "f", "a", "v"])
def test_qualified_nested_bindings(kind: str) -> None:
    entries = {
        "c": "c:pkg/__init__.py:outer:middle:inner:Record",
        "f": "f:pkg/__init__.py:outer:middle:inner",
        "a": "a:pkg/__init__.py:outer:middle:inner:value",
        "v": "v:pkg/__init__.py:outer:middle:inner:result",
    }
    result = obfuscate_modules(
        {
            Path("pkg/__init__.py"): (
                "def outer():\n"
                " def middle():\n"
                "  def inner(value):\n"
                "   class Record: pass\n"
                "   result = Record\n"
                "   return value, result.__name__\n"
                "  return inner(value=7)\n"
                " return middle()\n"
                "__result__ = outer()\n"
            ),
        },
        [entries[kind]],
        jobs=1,
    )
    # Automatic parameter renaming may already have selected the argument.
    if kind != "a":
        assert entries[kind] in result.renames
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"][0] == 7


def test_qualified_star_expansion_keeps_source_path() -> None:
    result = obfuscate_modules(
        {Path("pkg/worker.py"): "def helper(value): return value\n__result__ = helper(value=4)"},
        ["f:pkg/worker.py:helper*"],
        jobs=1,
    )
    assert "f:pkg/worker.py:helper*" in result.renames
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 4


def test_qualified_selector_still_rejects_rebinding() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        obfuscate_modules(
            {Path("worker.py"): "def run(): pass\ndef run(): pass"},
            ["f:worker.py:run"],
            jobs=1,
        )


def test_qualified_selector_does_not_match_other_files() -> None:
    with pytest.raises(ValueError, match="target not found"):
        obfuscate_modules(
            {Path("worker.py"): "def run(): pass"}, ["f:missing.py:run"], jobs=1
        )


def test_unqualified_collisions_remain_ambiguous() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        obfuscate_modules(
            {Path(f"{name}.py"): "def run(): pass" for name in ("first", "second")},
            ["f:run"],
            jobs=1,
        )


@pytest.mark.parametrize(
    ("source", "selector"),
    [
        ("class Record: pass\n__result__ = Record.__name__", "c:Record"),
        ("def run(value): return value\n__result__ = run(value=3)", "f:run*"),
        ("def run(value): return value\n__result__ = run(value=3)", "a:run:value"),
        ("VALUE = 3\n__result__ = VALUE", "v:VALUE"),
    ],
)
def test_all_qualified_kinds_rename_linked_collisions(source: str, selector: str) -> None:
    tree = flatten(
        {name: prepare_tree(source, f"{name}.py") for name in ("first", "second")},
        rename_classes=False,
    )
    kind, path = selector.split(":", 1)
    entries = [f"{kind}:{name}.py:{path}" for name in ("first", "second")]
    renames = AllowlistRenamer(tree).apply(entries)
    assert set(entries) <= renames.keys()
    assert renames[entries[0]] != renames[entries[1]]
    assert all(name in ast.unparse(tree) for name in renames.values())
    output = minify_tree(tree, "app.min.py", rename_globals=True)
    namespace: dict[str, Any] = {}
    exec(output, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    if kind == "c":
        assert namespace["__result__"] == renames[entries[0]]
    else:
        assert namespace["__result__"] == 3


def test_qualified_parameters_still_reject_escaping_callables() -> None:
    tree = flatten(
        {"worker": prepare_tree("def run(value): return value\nconsume(run)", "worker.py")}
    )
    with pytest.raises(ValueError, match="Escaping callable"):
        AllowlistRenamer(tree).apply(["f:worker.py:run*"])


def test_qualified_missing_scope_suggests_same_file_only() -> None:
    tree = flatten(
        {
            name: prepare_tree("class Record:\n def run(self): pass", f"{name}.py")
            for name in ("first", "second")
        },
        rename_classes=False,
    )
    with pytest.raises(ValueError, match=r"f:first\.py:Record:run") as error:
        AllowlistRenamer(tree).apply(["f:first.py:run"])
    assert "second.py" not in str(error.value)

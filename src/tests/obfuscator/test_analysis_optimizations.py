from __future__ import annotations

import ast
import pickle  # ruff: ignore[suspicious-pickle-import] - Test worker metadata serialization.
from typing import TYPE_CHECKING

import pytest

from obfuscator.analysis import annotation_nodes, inspects_frames
from obfuscator.annotations import TypeHint, merge_hints
from obfuscator.renamers.attribute_flow import Value, _TrackedMap
from obfuscator.renamers.base import ArgumentRenamer
from obfuscator.renamers.methods import PrivateMethodRenamer

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__: list[str] = []


def test_analysis_traversal_preserves_breadth_first_order_and_shared_nodes() -> None:
    tree = ast.parse("class Record:\n def method(self, value: int): return value\n")
    tree.body.append(tree.body[0])
    analysis = ArgumentRenamer({"fixture": tree})
    expected = list(ast.walk(tree))
    assert analysis.nodes == expected
    assert analysis.module_nodes == {"fixture": expected}
    assert analysis.parents == {
        child: node for node in expected for child in ast.iter_child_nodes(node)
    }
    assert annotation_nodes(expected) == annotation_nodes(tree)
    assert inspects_frames(expected) == inspects_frames(tree)


def test_preserved_parameters_do_not_scan_function_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse("def helper(long_parameter): return long_parameter\n")
    analysis = ArgumentRenamer({"fixture": tree})
    original_walk = ast.walk

    def checked_walk(node: ast.AST) -> Iterator[ast.AST]:
        assert not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        return original_walk(node)

    monkeypatch.setattr(ast, "walk", checked_walk)
    analysis.run()
    assert analysis.renamed == 0
    assert analysis.skipped == {"no resolved calls": 1}


def test_enclosing_classes_cache_nested_and_missing_owners() -> None:
    tree = ast.parse(
        "outside = 1\n"
        "class Outer:\n"
        " def method(self):\n"
        "  def inner(): return self\n"
        " class Nested:\n"
        "  def method(self): return self\n"
    )
    analysis = ArgumentRenamer({"fixture": tree})
    outer = tree.body[1]
    assert isinstance(outer, ast.ClassDef)
    nested = outer.body[1]
    assert isinstance(nested, ast.ClassDef)
    for node in analysis.nodes:
        scope = analysis.parents.get(node)
        while scope is not None and not isinstance(scope, ast.ClassDef):
            scope = analysis.parents.get(scope)
        assert analysis.enclosing_class(node) is scope
    # Cached results must not climb the same lexical chain on repeated queries.
    analysis.parents.clear()
    assert analysis.enclosing_class(outer.body[0]) is outer
    assert analysis.enclosing_class(nested.body[0]) is nested
    assert analysis.enclosing_class(tree.body[0]) is None


def test_parameters_reuse_method_indexes_with_identical_renames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "class Record:\n"
        " def _calculate(self, long_parameter): return long_parameter + 1\n"
        " def public(self, public_parameter):\n"
        "  return self._calculate(long_parameter=public_parameter)\n"
        "record = Record()\n"
        "__result__ = record.public(public_parameter=4)\n"
    )
    expected = ast.parse(source)
    fresh = PrivateMethodRenamer({"fixture": expected})
    fresh.run()
    parameters = ArgumentRenamer({"fixture": expected})
    parameters.run()

    actual = ast.parse(source)
    reused = PrivateMethodRenamer({"fixture": actual})
    reused.run()
    assert reused.renamed == fresh.renamed == 1

    def unexpected_init(self: ArgumentRenamer, trees: dict[str, ast.Module]) -> None:
        pytest.fail("Parameter renaming rebuilt the lexical indexes")

    monkeypatch.setattr(ArgumentRenamer, "__init__", unexpected_init)
    reused.rename_parameters()
    assert reused.renamed == parameters.renamed > 0
    assert reused.skipped == parameters.skipped
    assert ast.dump(actual) == ast.dump(expected)


def test_type_hints_retain_nested_shape_and_incompleteness() -> None:
    leaf = TypeHint(("fixture.Record",), incomplete=True)
    hint = TypeHint(origin="tuple", arguments=(leaf, leaf), variadic=True)
    assert pickle.loads(pickle.dumps(hint)) == hint  # ruff: ignore[suspicious-pickle-usage] - Fixed metadata.
    merged = merge_hints(hint, hint)
    assert merged.arguments == (leaf,)
    assert merged.variadic
    assert merge_hints(TypeHint(incomplete=True), leaf).incomplete


def test_member_lookup_caches_hits_and_misses_until_renames() -> None:
    tree = ast.parse(
        "class Parent:\n def _method(self): return 1\n"
        "class Child(Parent): pass\n"
    )
    analysis = ArgumentRenamer({"fixture": tree})
    parent, child = tree.body
    assert isinstance(parent, ast.ClassDef)
    assert isinstance(child, ast.ClassDef)
    binding = analysis.member_binding(child, "_method")
    assert binding is not None
    assert analysis.member_binding(child, "missing") is None
    assert analysis._member_bindings == {(child, "_method"): binding, (child, "missing"): None}
    binding.rename("renamed")
    analysis.run()
    assert analysis.member_binding(child, "_method") is None
    assert analysis.member_binding(child, "renamed") is binding


def test_mro_cache_does_not_reuse_path_dependent_cycle_results() -> None:
    tree = ast.parse("class First(Second): pass\nclass Second(First): pass\n")
    analysis = ArgumentRenamer({"fixture": tree})
    first, second = tree.body
    assert isinstance(first, ast.ClassDef)
    assert isinstance(second, ast.ClassDef)
    assert analysis.class_mro(first, set()) == [first, second]
    assert analysis.class_mro(second, set()) == [second, first]
    assert analysis.class_mro(first, {id(second)}) == [first]


def test_receiver_joins_reuse_unchanged_values_without_losing_alternatives() -> None:
    first = ast.ClassDef(name="First", bases=[], keywords=[], body=[], decorator_list=[])
    second = ast.ClassDef(name="Second", bases=[], keywords=[], body=[], decorator_list=[])
    known = Value(frozenset({first}))
    wider = Value(frozenset({first, second}), unknown=True)
    assert wider.merge(known) is wider
    assert known.merge(wider) is wider
    assert known.merge(Value(frozenset({first}))) is known
    assert known.merge(Value(unknown=True)) == Value(frozenset({first}), unknown=True)


def test_tracked_map_reports_present_and_missing_reads() -> None:
    reads: list[tuple[str, object]] = []
    writes: list[tuple[str, object]] = []
    tracked = _TrackedMap[str, int](
        "fields", lambda category, key: reads.append((category, key)),
        lambda category, key: writes.append((category, key)),
    )
    tracked["present"] = 3
    assert tracked.get("present") == 3
    assert tracked.get("missing", 7) == 7
    assert "missing" not in tracked
    assert tracked["present"] == 3
    assert reads == [("fields", key) for key in ("present", "missing", "missing", "present")]
    assert writes == [("fields", "present")]

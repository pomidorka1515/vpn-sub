from __future__ import annotations

import ast
import pickle  # ruff: ignore[suspicious-pickle-import] - Test worker metadata serialization.
from typing import TYPE_CHECKING

from obfuscator.analysis import annotation_nodes, inspects_frames
from obfuscator.annotations import TypeHint, merge_hints
from obfuscator.renamers.attribute_flow import Value
from obfuscator.renamers.base import ArgumentRenamer

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

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


def test_type_hints_retain_nested_shape_and_incompleteness() -> None:
    leaf = TypeHint(("fixture.Record",), incomplete=True)
    hint = TypeHint(origin="tuple", arguments=(leaf, leaf), variadic=True)
    assert pickle.loads(pickle.dumps(hint)) == hint  # ruff: ignore[suspicious-pickle-usage] - Fixed metadata.
    merged = merge_hints(hint, hint)
    assert merged.arguments == (leaf,)
    assert merged.variadic
    assert merge_hints(TypeHint(incomplete=True), leaf).incomplete


def test_receiver_joins_reuse_unchanged_values_without_losing_alternatives() -> None:
    first = ast.ClassDef(name="First", bases=[], keywords=[], body=[], decorator_list=[])
    second = ast.ClassDef(name="Second", bases=[], keywords=[], body=[], decorator_list=[])
    known = Value(frozenset({first}))
    wider = Value(frozenset({first, second}), unknown=True)
    assert wider.merge(known) is wider
    assert known.merge(wider) is wider
    assert known.merge(Value(frozenset({first}))) is known
    assert known.merge(Value(unknown=True)) == Value(frozenset({first}), unknown=True)

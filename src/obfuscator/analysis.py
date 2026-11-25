from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__: list[str] = ["Binding", "Function", "annotation_nodes", "inspects_frames"]


type Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def annotation_nodes(tree: ast.Module | Iterable[ast.AST]) -> set[int]:
    """Node identities inside annotation subtrees, which are release metadata."""
    excluded: set[int] = set()
    for node in ast.walk(tree) if isinstance(tree, ast.Module) else tree:
        root: ast.expr | None = None
        if isinstance(node, ast.arg) and node.annotation is not None:
            root = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            root = node.returns
        elif isinstance(node, ast.AnnAssign):
            root = node.annotation
        elif isinstance(node, ast.TypeAlias):
            root = node.value
        if root is not None:
            excluded.update(id(child) for child in ast.walk(root))
    return excluded


def inspects_frames(tree: ast.Module | Iterable[ast.AST]) -> bool:
    """Frame/stack APIs can expose names in any active lexical scope."""
    frame_apis = {
        "_getframe",
        "_current_frames",
        "currentframe",
        "settrace",
        "setprofile",
        "f_locals",
        "f_globals",
        "co_varnames",
    }
    return any(
        (isinstance(node, ast.Attribute) and node.attr in frame_apis)
        or (isinstance(node, ast.alias) and node.name in frame_apis)
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dir"
            and not node.args
            and not node.keywords
        )
        for node in (ast.walk(tree) if isinstance(tree, ast.Module) else tree)
    )


class Binding(Protocol):
    """The lexical binding interface provided by pinned python-minifier 3.4.0."""

    name: str
    references: list[ast.AST]

    def rename(self, new_name: str) -> None: ...

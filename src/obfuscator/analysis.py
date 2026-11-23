from __future__ import annotations

import ast
from typing import Protocol

__all__: list[str] = ["Binding", "Function", "inspects_frames"]


type Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def inspects_frames(tree: ast.Module) -> bool:
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
        for node in ast.walk(tree)
    )


class Binding(Protocol):
    """The lexical binding interface provided by pinned python-minifier 3.4.0."""

    name: str
    references: list[ast.AST]

    def rename(self, new_name: str) -> None: ...

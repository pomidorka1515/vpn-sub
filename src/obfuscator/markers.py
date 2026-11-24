from __future__ import annotations

import ast
import sys
from collections.abc import Collection
from typing import cast

from .renamers.base import ArgumentRenamer

__all__: list[str] = ["strip_markers"]


def strip_markers(tree: ast.Module, filename: str) -> None:
    """Resolve imported marker bindings, record protection, and erase build hints."""
    if not any(
        (isinstance(node, ast.ImportFrom) and node.module == "obfuscator")
        or (isinstance(node, ast.Import) and any(a.name == "obfuscator" for a in node.names))
        for node in ast.walk(tree)
    ):
        return
    analysis = ArgumentRenamer({"__main__": tree})
    removed: set[ast.AST] = set()

    def marker(expression: ast.expr) -> str | None:
        name: str | None = None
        reference = expression
        if isinstance(expression, ast.Attribute):
            name = expression.attr
            reference = expression.value
        binding = analysis.bindings.get(reference)
        if binding is None:
            return None
        definitions = analysis.definitions(binding)
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            return None
        module, imported = analysis.imports[definitions[0]]
        if module != "obfuscator":
            return None
        resolved = imported if name is None else name if imported is None else None
        return resolved if resolved in {"no_rename", "no_rename_arg"} else None

    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        kept: list[ast.expr] = []
        for decorator in node.decorator_list:
            kind = marker(decorator.func if isinstance(decorator, ast.Call) else decorator)
            if kind is None:
                kept.append(decorator)
                continue
            location = f"{filename}:{node.lineno}: {node.name}"
            if kind == "no_rename":
                if isinstance(decorator, ast.Call):
                    raise ValueError(f"{location}: use @no_rename without parentheses")
                node.__dict__["no_rename"] = True
            else:
                if (
                    isinstance(node, ast.ClassDef)
                    or not isinstance(decorator, ast.Call)
                    or len(decorator.args) != 1
                    or decorator.keywords
                ):
                    raise ValueError(f"{location}: no_rename_arg requires a function and literal names")
                try:
                    raw_names: object = ast.literal_eval(decorator.args[0])
                except (ValueError, TypeError, SyntaxError) as error:
                    raise ValueError(f"{location}: no_rename_arg requires literal names") from error
                if not isinstance(raw_names, (list, tuple, set, frozenset)) or not all(
                    isinstance(name, str) for name in cast(Collection[object], raw_names)
                ):
                    raise ValueError(f"{location}: no_rename_arg requires a collection of strings")
                names = set(cast(Collection[str], raw_names))
                parameters = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                parameters += [arg for arg in (node.args.vararg, node.args.kwarg) if arg]
                if "*" in names and len(names) > 1:
                    print(
                        f"Warning: {location}: no_rename_arg mixes '*' with other names (pointless)",
                        file=sys.stderr,
                    )
                for name in sorted(set(names) - {"*"} - {arg.arg for arg in parameters}):
                    print(
                        f"Warning: {location}: no_rename_arg argument {name!r} does not exist; skipped",
                        file=sys.stderr,
                    )
                for argument in parameters:
                    if "*" in names or argument.arg in names:
                        argument.__dict__["no_rename"] = True
            removed.update(ast.walk(decorator))
        node.decorator_list = kept

    # Erase only imports used exclusively by stripped decorators. Other imports
    # in the same statement, and shadowed/non-marker uses, retain their meaning.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            node.names = [
                alias
                for alias in node.names
                if (binding := analysis.bindings.get(alias)) is None
                or analysis.imports[alias][0] != "obfuscator"
                or not all(ref is alias or ref in removed for ref in binding.references)
            ]

    class EmptyImports(ast.NodeTransformer):
        def visit_Import(self, node: ast.Import) -> ast.Import | None:
            return node if node.names else None

        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom | None:
            return node if node.names else None

    EmptyImports().visit(tree)

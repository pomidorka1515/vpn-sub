from __future__ import annotations

import ast
import itertools
import string
import sys
from collections import defaultdict
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from .analysis import inspects_frames
from .renamers.base import ArgumentRenamer

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from .analysis import Binding

__all__: list[str] = ["consolidate_aliases", "shorten_imports"]


def fresh_names(tree: ast.Module, nodes: Iterable[ast.AST] | None = None) -> Iterator[str]:
    used: set[str] = set()
    for node in ast.walk(tree) if nodes is None else nodes:
        used.update(binding.name for binding in ArgumentRenamer.node_bindings(node))
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.arg):
            used.add(node.arg)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            used.add(node.name)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.AnnAssign):
            for reference in ast.walk(node.annotation):
                if isinstance(reference, ast.Constant) and isinstance(reference.value, str):
                    with suppress(SyntaxError):
                        expression = ast.parse(reference.value, mode="eval")
                        used.update(
                            name.id for name in ast.walk(expression) if isinstance(name, ast.Name)
                        )
    for width in itertools.count(1):
        for letters in itertools.product(string.ascii_uppercase, repeat=width):
            name = "".join(letters)
            if name not in used:
                used.add(name)
                yield name


def rewrite_quoted(annotation: ast.AST, names: dict[str, str], *, parsed: bool = False) -> None:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        with suppress(SyntaxError):
            expression = ast.parse(annotation.value, mode="eval")
            rewrite_quoted(expression, names, parsed=True)
            annotation.value = ast.unparse(expression)
        return
    if isinstance(annotation, ast.Name):
        if parsed and annotation.id in names:
            annotation.id = names[annotation.id]
        return
    # Literal values and Annotated metadata are data, not type names.
    if isinstance(annotation, ast.Subscript):
        spelling = ast.unparse(annotation.value).rsplit(".", 1)[-1]
        if spelling == "Literal":
            return
        if spelling == "Annotated" and isinstance(annotation.slice, ast.Tuple):
            rewrite_quoted(annotation.slice.elts[0], names, parsed=parsed)
            return
    for child in ast.iter_child_nodes(annotation):
        rewrite_quoted(child, names, parsed=parsed)


def rename_bindings(
    tree: ast.Module, analysis: ArgumentRenamer, replacements: dict[Binding, str]
) -> None:
    if not replacements:
        return
    # Quoted field annotations are evaluated later by dataclasses/dacite. They
    # are not references in the lexical resolver, so update them explicitly.
    for node in analysis.nodes:
        if not isinstance(node, ast.AnnAssign):
            continue
        scope = cast(ast.AST, node.__dict__["namespace"])
        visible: dict[str, Binding] = {}
        while True:
            for binding in analysis.node_bindings(scope):
                visible.setdefault(binding.name, binding)
            if isinstance(scope, ast.Module):
                break
            scope = cast(ast.AST, scope.__dict__["namespace"])
        names = {
            name: replacements[binding]
            for name, binding in visible.items()
            if binding in replacements
        }

        # Real ast.Name references are renamed by binding.rename below.
        rewrite_quoted(node.annotation, names)
    for binding, name in replacements.items():
        binding.rename(name)


def shorten_imports(tree: ast.Module) -> None:
    """Alias every packed import independently of name-based preservation."""
    if inspects_frames(tree):
        return
    analysis = ArgumentRenamer({"__main__": tree})
    if getattr(tree, "tainted", False):
        return
    fresh = fresh_names(tree, analysis.nodes)
    replacements: dict[Binding, str] = {}
    for binding in analysis.symbols["__main__"].values():
        definitions = analysis.definitions(binding)
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            continue
        module, _ = analysis.imports[definitions[0]]
        if module != "__future__":
            replacements[binding] = next(fresh)
    rename_bindings(tree, analysis, replacements)


def consolidate_aliases(tree: ast.Module) -> None:
    """Hoist repeated, single-assignment aliases of builtins/stdlib values."""
    if inspects_frames(tree):
        return
    analysis = ArgumentRenamer({"__main__": tree})
    if getattr(tree, "tainted", False):
        return
    groups: dict[tuple[str, object], list[tuple[ast.Assign, Binding]]] = defaultdict(list)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id.startswith("__"):
            continue
        binding = analysis.bindings[target]
        if analysis.definitions(binding) != [target]:
            continue
        value = statement.value
        if isinstance(value, ast.Name):
            source = analysis.bindings[value]
            definitions = analysis.definitions(source)
            if not isinstance(cast(object, source), BuiltinBinding):
                if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
                    continue
                module, _ = analysis.imports[definitions[0]]
                if module.split(".")[0] not in sys.stdlib_module_names:
                    continue
            key: tuple[str, object] = ("name", source)
        elif isinstance(value, ast.Constant) and (
            value.value is None or value.value is Ellipsis or isinstance(value.value, bool)
        ):
            key = ("singleton", value.value)
        else:
            continue
        groups[key].append((statement, binding))
    fresh = fresh_names(tree, analysis.nodes)
    replacements: dict[Binding, str] = {}
    removed: set[ast.stmt] = set()
    hoisted: list[ast.stmt] = []
    for aliases in groups.values():
        if len(aliases) < 2:
            continue
        name = next(fresh)
        hoisted.append(
            ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=aliases[0][0].value)
        )
        for statement, binding in aliases:
            replacements[binding] = name
            removed.add(statement)
    rename_bindings(tree, analysis, replacements)
    body = [statement for statement in tree.body if statement not in removed]
    index = 0
    while index < len(body) and isinstance(body[index], (ast.Import, ast.ImportFrom)):
        index += 1
    tree.body = body[:index] + hoisted + body[index:]
    ast.fix_missing_locations(tree)

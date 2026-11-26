from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from python_minifier.ast_annotation import add_parent
from python_minifier.rename.mapper import add_namespace
from python_minifier.transforms.constant_folding import FoldConstants
from python_minifier.transforms.remove_dead_branches import RemoveDeadBranches
from python_minifier.transforms.remove_pass import RemovePass

__all__: list[str] = ["annotate_debug", "record_attribute"]

type Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def record_attribute(owner: ast.ClassDef, original: str, renamed: str) -> None:
    attributes = cast(dict[str, str], owner.__dict__.setdefault("debug_attributes", {}))
    attributes[renamed] = attributes.pop(original, original)


def _definitions(tree: ast.AST) -> list[Definition]:
    result: list[Definition] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return result


def _surviving_definitions(tree: ast.Module) -> list[Definition]:
    # Mirror the minifier's branch pruning on a fresh tree so definitions in
    # removed branches cannot shift the original-name mappings of later ones.
    shadow = ast.parse(ast.unparse(tree))
    originals = dict(zip(_definitions(shadow), _definitions(tree), strict=True))
    for operation in (add_parent, add_namespace):
        cast(Callable[[ast.Module], None], operation)(shadow)
    for factory in (RemovePass, FoldConstants, RemoveDeadBranches):
        transform = cast(Callable[[], object], factory)()
        shadow = cast(Callable[[ast.Module], ast.Module], transform)(shadow)
    return [originals[node] for node in _definitions(shadow)]


def annotate_debug(source: str, tree: ast.Module, filename: str) -> str:
    """Insert comments into minified text without rewriting any executable text."""
    lines = source.splitlines(keepends=True)
    comments: dict[int, list[str]] = {}

    def add(line: int, indent: str, kind: str, renamed: str, original: str) -> None:
        if renamed != original:
            comments.setdefault(line, []).append(f"{indent}# {kind} {renamed} {original}\n")

    for original, emitted in zip(
        _surviving_definitions(tree), _definitions(ast.parse(source)), strict=True
    ):
        if type(original) is not type(emitted) or original.name != emitted.name:
            raise ValueError("Minification changed debug definition order")
        first = min([emitted.lineno, *(item.lineno for item in emitted.decorator_list)])
        line = lines[first - 1]
        indent = line[: len(line) - len(line.lstrip())]
        add(
            first,
            indent,
            "class" if isinstance(emitted, ast.ClassDef) else "func",
            emitted.name,
            cast(str, original.__dict__.get("original_name", original.name)),
        )
        if isinstance(original, ast.ClassDef) and isinstance(emitted, ast.ClassDef):
            statement = emitted.body[0]
            body_line = statement.lineno
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body_line = min([body_line, *(item.lineno for item in statement.decorator_list)])
            if body_line == emitted.lineno:
                # A compact class has no separate body line. A comment directly
                # after its suite still groups its fields at class indentation.
                body_line += 1
                body_indent = indent + "\t"
            else:
                body = lines[body_line - 1]
                body_indent = body[: len(body) - len(body.lstrip())]
            attributes = cast(dict[str, str], original.__dict__.get("debug_attributes", {}))
            for renamed, name in attributes.items():
                add(body_line, body_indent, "attr", renamed, name)
        elif isinstance(original, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = original.args
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *([arguments.vararg] if arguments.vararg else []),
                *arguments.kwonlyargs,
                *([arguments.kwarg] if arguments.kwarg else []),
            ):
                add(
                    first,
                    indent,
                    "arg",
                    argument.arg,
                    cast(str, argument.__dict__.get("original_name", argument.arg)),
                )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    output = [f"# {filename} {timestamp}\n"]
    for number, line in enumerate(lines, 1):
        output.extend(comments.get(number, []))
        output.append(line)
    output.extend(comments.get(len(lines) + 1, []))
    result = "".join(output)
    compile(result, filename, "exec")
    return result

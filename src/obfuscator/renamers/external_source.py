from __future__ import annotations

import ast
import builtins
import inspect
from typing import TYPE_CHECKING, cast

from ..annotations import TypeHint, merge_hints

if TYPE_CHECKING:
    from types import FunctionType

    from .external import ExternalReturns

__all__: list[str] = []


def source_return(
    analysis: ExternalReturns,
    function: FunctionType,
    definition: ast.FunctionDef | ast.AsyncFunctionDef,
    receiver: TypeHint,
) -> TypeHint:
    """Infer closed return expressions from Python source, never run the body.

    This deliberately does not interpret arbitrary Python. Every return path
    and local definition must be understood; recursion and opaque values are
    unknown. Constructor arguments are not evaluated to identify their result.
    """
    if not definition.body or not isinstance(definition.body[-1], (ast.Return, ast.Raise)):
        return TypeHint()
    nodes: list[ast.AST] = list(definition.body)
    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            nodes.extend(ast.iter_child_nodes(node))  # ruff: ignore[loop-iterator-mutation] - Syntax traversal.
    assignments: dict[str, list[ast.expr | None]] = {}
    for node in nodes:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) else []
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    value = node.value if target is name and isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) else None
                    assignments.setdefault(name.id, []).append(value)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    assignments.setdefault(name.id, []).append(None)
    namespace = function.__globals__
    positional = [*definition.args.posonlyargs, *definition.args.args]
    bound_names = {argument.arg for argument in [*positional, *definition.args.kwonlyargs]}
    bound_names.update(node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    bound_names.update(node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    bound_names.update(
        alias.asname or alias.name.split(".")[0]
        for node in nodes if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    if definition.args.vararg is not None:
        bound_names.add(definition.args.vararg.arg)
    if definition.args.kwarg is not None:
        bound_names.add(definition.args.kwarg.arg)
    receiver_name = positional[0].arg if positional and receiver else None

    def joined(hints: list[TypeHint]) -> TypeHint:
        return merge_hints(*hints) if hints and all(hint and not hint.incomplete for hint in hints) else TypeHint()

    def resolve(expression: ast.expr) -> object | None:
        if isinstance(expression, ast.Name) and expression.id not in assignments and expression.id not in bound_names:
            return namespace.get(expression.id, vars(builtins).get(expression.id))
        if isinstance(expression, ast.Attribute):
            # Use the existing static lookup implementation, not descriptors.
            parent = resolve(expression.value)
            if parent is not None:
                return cast(object, inspect.getattr_static(parent, expression.attr, None))
        return None

    def infer(expression: ast.expr | None, seen: frozenset[str] = frozenset()) -> TypeHint:
        if expression is None:
            return TypeHint()
        if isinstance(expression, ast.Name):
            if expression.id == receiver_name:
                return receiver
            if expression.id in seen:
                return TypeHint()
            values = assignments.get(expression.id, [])
            return joined([infer(value, seen | {expression.id}) for value in values])
        if isinstance(expression, ast.Constant):
            return analysis.runtime_hint(type(expression.value))
        if isinstance(expression, ast.Tuple):
            hints = tuple(infer(element, seen) for element in expression.elts)
            return TypeHint(origin="tuple", arguments=hints) if all(hints) else TypeHint()
        if isinstance(expression, ast.IfExp):
            return joined([infer(expression.body, seen), infer(expression.orelse, seen)])
        if isinstance(expression, ast.Await):
            value = infer(expression.value, seen)
            return value.element() if value.origin == "awaitable" else TypeHint()
        if isinstance(expression, ast.Subscript):
            value = infer(expression.value, seen)
            index = expression.slice.value if isinstance(expression.slice, ast.Constant) and isinstance(expression.slice.value, int) else None
            return value.element(index)
        if isinstance(expression, ast.Call):
            target = resolve(expression.func)
            if isinstance(target, type):
                return analysis.runtime_hint(target)
            if target is not None:
                return analysis.returned(target, call=expression)
            if isinstance(expression.func, ast.Attribute):
                return analysis.method(infer(expression.func.value, seen), expression.func.attr)
        return TypeHint()

    return joined([infer(node.value) for node in nodes if isinstance(node, ast.Return)])

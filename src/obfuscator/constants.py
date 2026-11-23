from __future__ import annotations

import ast
import math
import operator
from typing import TYPE_CHECKING, Any, cast

from .analysis import inspects_frames
from .renamers.base import ArgumentRenamer

if TYPE_CHECKING:
    from collections.abc import Callable

    from .analysis import Binding

__all__: list[str] = ["inline_constants"]

type LiteralValue = str | int | float
type BinaryOperation = Callable[[Any, Any], object]


def binary_operation(name: str) -> BinaryOperation:
    # Several operator stubs have unbound generic arguments. Establish the
    # evaluator's callable interface at this stdlib boundary.
    return cast(BinaryOperation, getattr(operator, name))


_BINARY: dict[type[ast.operator], BinaryOperation] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: binary_operation("truediv"),
    ast.FloorDiv: binary_operation("floordiv"),
    ast.Mod: operator.mod,
    ast.Pow: binary_operation("pow"),
    ast.LShift: binary_operation("lshift"),
    ast.RShift: binary_operation("rshift"),
    ast.BitOr: binary_operation("or_"),
    ast.BitAnd: binary_operation("and_"),
    ast.BitXor: binary_operation("xor"),
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], object]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
}


def short_literal(value: object) -> LiteralValue | None:
    if type(value) not in {str, int, float}:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float)) and len(str(value)) < 150:
        return value
    return None


def evaluate(
    node: ast.expr, analysis: ArgumentRenamer, values: dict[Binding, LiteralValue]
) -> LiteralValue | None:
    """Evaluate only bounded literal operations, never application code."""
    if isinstance(node, ast.Constant):
        return short_literal(node.value)
    if isinstance(node, ast.Name):
        binding = analysis.bindings.get(node)
        return values.get(binding) if binding is not None else None
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        operand = evaluate(node.operand, analysis, values)
        if operand is not None:
            return short_literal(_UNARY[type(node.op)](operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left = evaluate(node.left, analysis, values)
        right = evaluate(node.right, analysis, values)
        if left is None or right is None:
            return None
        # Bound expensive operations before performing them. String %-formatting
        # can allocate arbitrary widths and is intentionally not evaluated.
        if isinstance(node.op, ast.Mod) and isinstance(left, str):
            return None
        if isinstance(node.op, ast.Mult) and (isinstance(left, str) or isinstance(right, str)):
            text, count = (left, right) if isinstance(left, str) else (right, left)
            if not isinstance(text, str) or not isinstance(count, int):
                return None
            if len(text) * max(count, 0) >= 150:
                return None
        if isinstance(node.op, (ast.Pow, ast.LShift, ast.RShift)) and (
            not isinstance(right, (int, float)) or abs(right) > 512
        ):
            return None
        return short_literal(_BINARY[type(node.op)](left, right))
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue) and part.format_spec is None:
                value = evaluate(part.value, analysis, values)
                if value is None:
                    return None
                conversions: dict[int, Callable[[object], str]] = {
                    -1: str,
                    115: str,
                    114: repr,
                    97: ascii,
                }
                conversion = conversions.get(part.conversion)
                if conversion is None:
                    return None
                parts.append(conversion(value))
            else:
                return None
            if sum(map(len, parts)) >= 150:
                return None
        return short_literal("".join(parts))
    return None


def inline_constants(tree: ast.Module) -> None:
    """Inline short, single-assignment module literals after onefile linking."""
    if inspects_frames(tree):
        return
    analysis = ArgumentRenamer({"__main__": tree})
    if getattr(tree, "tainted", False):
        return
    # Quoted annotations resolve outside the AST binding graph. Retain their
    # dependencies rather than treating arbitrary annotation strings as code.
    quoted_names: set[str] = set()
    annotations = [node.annotation for node in ast.walk(tree) if isinstance(node, ast.AnnAssign)]
    while annotations:
        annotation = annotations.pop()
        for reference in ast.walk(annotation):
            if isinstance(reference, ast.Constant) and isinstance(reference.value, str):
                try:
                    expression = ast.parse(reference.value, mode="eval")
                except SyntaxError:
                    continue
                annotations.append(expression.body)
                quoted_names.update(
                    name.id for name in ast.walk(expression) if isinstance(name, ast.Name)
                )
    values: dict[Binding, LiteralValue] = {}
    removed: set[ast.stmt] = set()
    positions: dict[ast.AST, int] = {statement: index for index, statement in enumerate(tree.body)}

    def before_definition(reference: ast.AST, position: int) -> bool:
        owner = reference
        while analysis.parents.get(owner) is not tree:
            owner = analysis.parents[owner]
        return positions[owner] <= position

    def class_self_alias(reference: ast.AST, binding: Binding) -> bool:
        # The minifier merges `class C: VALUE = VALUE`'s attribute store into
        # the global binding to prevent renaming the attribute. It is not a
        # reassignment of the module constant; retain the store, inline its RHS.
        if not isinstance(reference, ast.Name) or not isinstance(reference.ctx, ast.Store):
            return False
        scope = reference.__dict__["namespace"]
        assignment = analysis.parents.get(reference)
        return (
            isinstance(scope, ast.ClassDef)
            and isinstance(assignment, ast.Assign)
            and assignment in scope.body
            and assignment.targets == [reference]
            and isinstance(assignment.value, ast.Name)
            and analysis.bindings.get(assignment.value) is binding
        )

    for position, statement in enumerate(tree.body):
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            expression_value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target = statement.target
            expression_value = statement.value
        else:
            continue
        if (
            not isinstance(target, ast.Name)
            or target.id in quoted_names
            or (target.id.startswith("__") and target.id.endswith("__"))
        ):
            continue
        binding = analysis.bindings[target]
        definitions = [
            definition
            for definition in analysis.definitions(binding)
            if not class_self_alias(definition, binding)
        ]
        if definitions != [target]:
            continue
        references = [
            reference
            for reference in binding.references
            if reference is not target and not class_self_alias(reference, binding)
        ]
        if any(
            not isinstance(reference, ast.Name)
            or not isinstance(reference.ctx, ast.Load)
            or before_definition(reference, position)
            for reference in references
        ):
            continue
        try:
            value = evaluate(expression_value, analysis, values)
        except ArithmeticError, TypeError, ValueError, RecursionError:
            continue
        if value is not None:
            values[binding] = value
            removed.add(statement)

    class Inline(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.expr:
            binding = analysis.bindings.get(node)
            if isinstance(node.ctx, ast.Load) and binding is not None and binding in values:
                return ast.copy_location(ast.Constant(value=values[binding]), node)
            return node

    tree.body = [statement for statement in tree.body if statement not in removed]
    Inline().visit(tree)
    ast.fix_missing_locations(tree)

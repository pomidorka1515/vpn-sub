from __future__ import annotations

import ast
from dataclasses import replace
from typing import TYPE_CHECKING

from .annotations import TypeHint, merge_hints

if TYPE_CHECKING:
    from collections.abc import Callable

__all__: list[str] = ["bind_call", "join_hints", "operation_hint", "unify_hint"]


def join_hints(hints: list[TypeHint]) -> TypeHint:
    """Join actual alternatives without silently dropping an opaque value."""
    result = merge_hints(*hints)
    return replace(result, incomplete=True) if any(not hint or hint.incomplete for hint in hints) else result


def unify_hint(template: TypeHint, actual: TypeHint, substitutions: dict[str, TypeHint]) -> None:
    """Collect structural generic constraints, retaining unknown alternatives."""
    if template.origin == "parameter" and template.classes:
        key = template.classes[0]
        candidates = [actual]
        if key in substitutions:
            candidates.insert(0, substitutions[key])
        substitutions[key] = join_hints(candidates)
        return
    if template.origin == actual.origin and template.classes == actual.classes:
        if template.origin == "tuple" and template.variadic:
            pairs = [(template.arguments[-1], actual.iterated())] if template.arguments else []
        else:
            pairs = [
                (argument, actual.arguments[index] if index < len(actual.arguments) else TypeHint(incomplete=True))
                for index, argument in enumerate(template.arguments)
            ]
    elif template.origin == "sequence" and actual.origin in {"sequence", "tuple", "mapping"}:
        pairs = list(zip(template.arguments, (actual.iterated(),), strict=False))
    else:
        # A shape mismatch must not allow a constraint from another argument
        # to disguise this unresolved occurrence of the same parameter.
        for argument in template.arguments:
            unify_hint(argument, TypeHint(incomplete=True), substitutions)
        return
    for left, right in pairs:
        unify_hint(left, replace(right, incomplete=True) if actual.incomplete else right, substitutions)


def bind_call(
    parameters: ast.arguments,
    call: ast.Call,
    infer: Callable[[ast.AST], TypeHint],
    *,
    bound: bool = False,
) -> dict[ast.arg, TypeHint] | None:
    """Bind visible argument shapes using Python's positional/keyword rules.

    Literal keyword expansions and fixed tuple expansions are structural, not
    evaluated. Unknown expansions or invalid calls cannot establish a binding.
    """
    positional = [*parameters.posonlyargs, *parameters.args]
    defaults = dict(zip(positional[len(positional) - len(parameters.defaults):], parameters.defaults, strict=True))
    defaults.update((parameter, default) for parameter, default in zip(parameters.kwonlyargs, parameters.kw_defaults, strict=True) if default is not None)
    if bound:
        positional = positional[1:]
    values: list[TypeHint] = []
    for argument in call.args:
        if isinstance(argument, ast.Starred):
            source = infer(argument.value)
            if source.origin != "tuple" or source.variadic or source.incomplete:
                return None
            values.extend(source.arguments)
        else:
            values.append(infer(argument))
    if len(values) > len(positional) and parameters.vararg is None:
        return None
    supplied = dict(zip(positional, values, strict=False))
    allowed = {parameter.arg: parameter for parameter in [*parameters.args, *parameters.kwonlyargs] if parameter in positional or parameter in parameters.kwonlyargs}
    keywords: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        if keyword.arg is not None:
            entries = [(keyword.arg, keyword.value)]
        elif isinstance(keyword.value, ast.Dict) and all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keyword.value.keys):
            entries = [(key.value, value) for key, value in zip(keyword.value.keys, keyword.value.values, strict=True) if isinstance(key, ast.Constant) and isinstance(key.value, str)]
        else:
            return None
        for name, value in entries:
            if name in keywords:
                return None
            keywords[name] = value
    for name, value in keywords.items():
        parameter = allowed.get(name)
        if parameter is None:
            if parameters.kwarg is None:
                return None
            continue
        if parameter in supplied:
            return None
        supplied[parameter] = infer(value)
    for parameter in [*positional, *parameters.kwonlyargs]:
        if parameter not in supplied:
            if parameter not in defaults:
                return None
            supplied[parameter] = infer(defaults[parameter])
    if parameters.vararg is not None:
        supplied[parameters.vararg] = join_hints(values[len(positional):])
    if parameters.kwarg is not None:
        supplied[parameters.kwarg] = join_hints([infer(value) for name, value in keywords.items() if name not in allowed])
    return supplied


def _awaited(hint: TypeHint) -> TypeHint:
    result = hint.element() if hint.origin == "awaitable" else TypeHint()
    return result or TypeHint(incomplete=True)


def operation_hint(
    call: ast.Call,
    path: str | None,
    infer: Callable[[ast.AST], TypeHint],
    iterate: Callable[[TypeHint], TypeHint] = TypeHint.iterated,
) -> TypeHint | None:
    """Structural transfer rules for lexically verified standard operations.

    None means no rule, not an unknown result. No application or external code
    is executed, and a same-spelled user function never selects a builtin rule.
    """
    arguments = call.args
    if path in {"contextlib.closing", "contextlib.aclosing", "contextlib.nullcontext"}:
        if len(arguments) > 1 or call.keywords:
            return TypeHint()
        item = infer(arguments[0]) if arguments else TypeHint(("types.NoneType",))
        return TypeHint(origin="async_contextmanager" if path == "contextlib.aclosing" else "contextmanager", arguments=(item,))
    if path in {"asyncio.gather", "asyncio.tasks.gather"}:
        results: list[TypeHint] = []
        expanded = False
        for argument in arguments:
            if isinstance(argument, ast.Starred):
                source = infer(argument.value)
                if source.origin == "tuple" and not source.variadic:
                    results.extend(_awaited(hint) for hint in source.arguments)
                else:
                    expanded = True
                    item = iterate(source)
                    results.append(_awaited(item))
            else:
                item = infer(argument)
                results.append(_awaited(item))
        exceptions = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "return_exceptions"),
            None,
        )
        if exceptions is not None and not (isinstance(exceptions, ast.Constant) and exceptions.value is False):
            results = [merge_hints(hint, TypeHint(("builtins.BaseException",))) for hint in results]
        result = (
            TypeHint(origin="sequence", arguments=(join_hints(results),))
            if expanded else TypeHint(origin="tuple", arguments=tuple(results))
        )
        return TypeHint(origin="awaitable", arguments=(result,))
    if path in {
        "asyncio.create_task", "asyncio.tasks.create_task", "asyncio.ensure_future",
        "asyncio.tasks.ensure_future", "asyncio.shield", "asyncio.tasks.shield",
        "asyncio.wait_for", "asyncio.tasks.wait_for",
    } and arguments:
        source = infer(arguments[0])
        return source if source.origin == "awaitable" else TypeHint()
    if path is None or not path.startswith("builtins."):
        return None
    name = path.removeprefix("builtins.")
    if name == "zip" and not any(isinstance(argument, ast.Starred) for argument in arguments):
        row = TypeHint(
            origin="tuple",
            arguments=tuple(iterate(infer(argument)) for argument in arguments),
        )
        return TypeHint(origin="sequence", arguments=(row,))
    if name == "enumerate" and arguments and not isinstance(arguments[0], ast.Starred):
        row = TypeHint(
            origin="tuple",
            arguments=(TypeHint(("builtins.int",)), iterate(infer(arguments[0]))),
        )
        return TypeHint(origin="sequence", arguments=(row,))
    if (
        name in {"list", "tuple", "set", "frozenset", "iter", "reversed", "sorted"}
        and len(arguments) == 1 and not isinstance(arguments[0], ast.Starred)
    ):
        source = infer(arguments[0])
        if name == "tuple" and source.origin == "tuple":
            return source
        return TypeHint(origin="sequence", arguments=(iterate(source),), incomplete=source.incomplete)
    if name == "next" and len(arguments) in {1, 2}:
        source = infer(arguments[0])
        hints = [iterate(source)]
        if len(arguments) == 2:
            hints.append(infer(arguments[1]))
        return join_hints(hints)
    if name == "dict" and len(arguments) == 1 and not call.keywords:
        source = infer(arguments[0])
        if source.origin == "mapping":
            return source
        pair = iterate(source)
        if pair.origin == "tuple" and len(pair.arguments) == 2 and not pair.variadic:
            return TypeHint(origin="mapping", arguments=pair.arguments)
    return None

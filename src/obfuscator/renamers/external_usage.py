from __future__ import annotations

import ast
import builtins
import copy
import inspect
import typing
from importlib import import_module
from types import FunctionType

from .external import _external_tree, _static_member

__all__: list[str] = []


class ExternalUsage:
    """Summarize which fields a visible external consumer reads from an argument.

    Captures in instance fields and aliases are followed across the whole class.
    Opaque forwarding and namespace reflection have no safe summary.
    """

    def __init__(self) -> None:
        self.cache: dict[tuple[type | FunctionType, str], frozenset[str] | None] = {}

    def fields(self, consumer: type | FunctionType, parameter: str,
               active: frozenset[tuple[type | FunctionType, str]] = frozenset()) -> frozenset[str] | None:
        key = consumer, parameter
        if key in active:
            return None
        if key in self.cache:
            return self.cache[key]
        try:
            result = self.summarize(consumer, parameter, active | {key})
        except (AttributeError, ImportError, OSError, TypeError, ValueError, SyntaxError):
            result = None
        self.cache[key] = result
        return result

    def summarize(self, consumer: type | FunctionType, parameter: str,
                  active: frozenset[tuple[type | FunctionType, str]]) -> frozenset[str] | None:
        tree = copy.deepcopy(_external_tree(consumer))
        root = tree.body[0]
        if isinstance(root, ast.ClassDef) and root.decorator_list:
            return None
        if isinstance(consumer, type) and isinstance(root, ast.ClassDef):
            for base in consumer.__mro__:
                if base is object:
                    continue
                if "__getattr__" in vars(base) or "__getattribute__" in vars(base):
                    return None
                if base is consumer:
                    continue
                for name, member in vars(base).items():
                    if name in {"__class_getitem__", "__init_subclass__", "__subclasshook__"}:
                        continue
                    if isinstance(member, FunctionType):
                        root.body.extend(copy.deepcopy(_external_tree(member)).body)
                    elif isinstance(member, property):
                        for accessor in (member.fget, member.fset, member.fdel):
                            if accessor is not None:
                                root.body.extend(copy.deepcopy(_external_tree(accessor)).body)
                    elif inspect.isroutine(member):
                        return None
        nodes = list(ast.walk(root))
        namespace = vars(import_module(consumer.__module__))
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Name):
                        return None
                    resolved = namespace.get(decorator.id, vars(builtins).get(decorator.id))
                    if not any(resolved is supported for supported in (property, classmethod, staticmethod, typing.overload)):
                        return None
        methods = {
            node.name: node for node in root.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        } if isinstance(root, ast.ClassDef) else {}
        receivers = {
            (method.args.posonlyargs + method.args.args)[0].arg
            for method in methods.values() if method.args.posonlyargs or method.args.args
            if not any(isinstance(decorator, ast.Name) and decorator.id == "staticmethod" for decorator in method.decorator_list)
        }

        def path(node: ast.AST) -> tuple[str, ...] | None:
            if isinstance(node, ast.Name):
                return ("<receiver>" if node.id in receivers else node.id,)
            if isinstance(node, ast.Attribute) and (prefix := path(node.value)) is not None:
                return *prefix, node.attr
            return None

        aliases: set[tuple[str, ...]] = {(parameter,)}
        while True:
            before = len(aliases)
            for node in nodes:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and path(node.value) in aliases:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        destination = path(target)
                        if destination is None:
                            return None
                        aliases.add(destination)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and (
                    path(node.func.value) == ("<receiver>",) and node.func.attr in methods
                ):
                    positional = methods[node.func.attr].args.posonlyargs + methods[node.func.attr].args.args
                    for argument, supplied in zip(positional[1:], node.args, strict=False):
                        if path(supplied) in aliases:
                            aliases.add((argument.arg,))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr"
                        and len(node.args) == 3 and path(node.args[2]) in aliases
                        and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                    owner = path(node.args[0])
                    if owner is None:
                        return None
                    aliases.add((*owner, node.args[1].value))
            if len(aliases) == before:
                break

        fields: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Attribute) and path(node.value) in aliases:
                if node.attr in {"__dict__", "__getattribute__", "__setattr__", "__delattr__", "__reduce__", "__reduce_ex__"}:
                    return None
                fields.add(node.attr)
            elif isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) and node.value is not None and path(node.value) in aliases:
                return None
            if not isinstance(node, ast.Call):
                continue
            passed = [(index, value) for index, value in enumerate(node.args) if path(value) in aliases]
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if path(keyword.value) in aliases}
            if not passed and not keywords:
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "setattr", "delattr", "hasattr"}:
                if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
                    return None
                if path(node.args[0]) not in aliases:
                    if node.func.id == "setattr" and len(node.args) == 3 and path(node.args[2]) in aliases:
                        continue
                    return None
                if node.func.id in {"setattr", "delattr"} or any(index != 0 for index, _ in passed):
                    return None
                fields.add(node.args[1].value)
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"type", "isinstance", "issubclass", "len", "bool", "hash", "str", "repr"} and node.func.id not in namespace:
                continue
            if isinstance(node.func, ast.Attribute) and path(node.func.value) == ("<receiver>",) and node.func.attr in methods:
                continue
            reference = node.func
            suffix: list[str] = []
            while isinstance(reference, ast.Attribute):
                suffix.insert(0, reference.attr)
                reference = reference.value
            if not isinstance(reference, ast.Name):
                return None
            helper = namespace.get(reference.id)
            for name in suffix:
                helper = _static_member(helper, name, None)
            if not isinstance(helper, (type, FunctionType)):
                return None
            signature = inspect.signature(helper, follow_wrapped=False, eval_str=False)
            bound = signature.bind_partial(*node.args, **{k.arg: k.value for k in node.keywords if k.arg is not None})
            for name, value in bound.arguments.items():
                if isinstance(value, ast.AST) and path(value) in aliases:
                    nested = self.fields(helper, name, active)
                    if nested is None:
                        return None
                    fields.update(nested)
        return frozenset(fields)

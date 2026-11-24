from __future__ import annotations

import ast
import builtins
import inspect
import re
from abc import ABCMeta
from importlib import import_module
from textwrap import dedent
from types import FunctionType, GetSetDescriptorType, MemberDescriptorType
from typing import cast

__all__: list[str] = []


class ExternalMembers:
    """Inspect installed bases without constructing them or evaluating base syntax.

    Scan implementations, not just class dictionaries: base methods can use
    instance-only fields or delegate to helpers/views.
    Unavailable Python implementations and dynamic receiver access fail closed.
    """

    def __init__(self) -> None:
        self.classes: dict[type, set[str]] = {}

    def resolve(self, module: str, path: list[str]) -> type:
        value: object = import_module(module)
        for name in path:
            value = inspect.getattr_static(value, name)
        if not isinstance(value, type):
            raise ValueError("external base is not a class")
        return value

    def members(self, owner: type) -> set[str]:
        if owner in self.classes:
            return self.classes[owner]
        if type(owner) not in {type, ABCMeta}:
            raise ValueError("external base has a custom metaclass")
        names: set[str] = set()
        seen: set[object] = set()
        for base in owner.__mro__:
            if base is object:
                continue
            namespace = vars(base)
            names.update(namespace)
            if any(name in namespace for name in (
                "__getattr__", "__getattribute__", "__setattr__", "__delattr__",
                "__new__", "__init_subclass__",
            )):
                raise ValueError("external base has dynamic receiver hooks")
            names.update(self.implementation_members(base, seen))
            for name, member in namespace.items():
                if name == "__class_getitem__":
                    continue  # Generic subscription does not dispatch instance members.
                if isinstance(member, (classmethod, staticmethod)):
                    member = cast(
                        classmethod[object, ..., object] | staticmethod[..., object], member
                    ).__func__
                if isinstance(member, FunctionType):
                    names.update(self.implementation_members(member, seen))
                elif isinstance(member, property):
                    for accessor in (member.fget, member.fset, member.fdel):
                        if accessor is not None:
                            if not isinstance(accessor, FunctionType):
                                raise ValueError("external property has an opaque implementation")
                            names.update(self.implementation_members(accessor, seen))
                elif inspect.isroutine(member):
                    raise ValueError(f"external member {name!r} has an opaque implementation")
                elif not isinstance(member, (MemberDescriptorType, GetSetDescriptorType)) and (
                    inspect.getattr_static(type(cast(object, member)), "__get__", None) is not None
                ):
                    raise ValueError(f"external member {name!r} is an unknown descriptor")
        self.classes[owner] = names
        return names

    def implementation_members(self, implementation: type | FunctionType, seen: set[object]) -> set[str]:
        if implementation in seen:
            return set()
        seen.add(implementation)
        module = inspect.getmodule(implementation)
        if module is None:
            raise ValueError("external base has no implementation module")
        tree = ast.parse(dedent(inspect.getsource(implementation)))
        names: set[str] = set()
        locals_ = {
            node.arg if isinstance(node, ast.arg) else node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.arg) or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        }
        receivers = {"self", "cls"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = node.args.posonlyargs + node.args.args
                if positional:
                    receivers.add(positional[0].arg)
        # Receiver aliases must not hide escapes or namespace inspection.
        while True:
            previous = receivers.copy()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name) and (
                    node.value.id in receivers
                ):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    receivers.update(target.id for target in targets if isinstance(target, ast.Name))
            if previous == receivers:
                break
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
                if node.attr in {"__dict__", "__getattribute__", "__setattr__", "__delattr__"} and (
                    isinstance(node.value, ast.Name) and node.value.id in receivers
                ):
                    raise ValueError("external implementation inspects receiver namespaces")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Literal reflection and helper-driven protocol checks also count.
                names.add(node.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"eval", "exec", "vars", "dir"}:
                    raise ValueError("external implementation uses dynamic reflection")
                if node.func.id in {"getattr", "setattr", "delattr", "hasattr"} and (
                    len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)
                    or not isinstance(node.args[1].value, str)
                ):
                    raise ValueError("external implementation uses dynamic attribute keys")
            if isinstance(node, ast.Call) and any(
                isinstance(argument, ast.Name) and argument.id in receivers
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
            ):
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and (
                    node.func.value.id in receivers
                ):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr == "format" and (
                    isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)
                ):
                    names.update(re.findall(r"[a-zA-Z_]\w*", node.func.value.value))
                    continue
                if not isinstance(node.func, ast.Name):
                    raise ValueError("external receiver escapes to an unresolved helper")
                if node.func.id in locals_:
                    raise ValueError("external receiver escapes to a dynamic local helper")
                helper = vars(module).get(node.func.id)
                if isinstance(helper, (type, FunctionType)) and helper.__module__ != "builtins":
                    implementations = helper.__mro__[:-1] if isinstance(helper, type) else (helper,)
                    for dependency in implementations:
                        names.update(self.implementation_members(dependency, seen))
                elif helper is not None or node.func.id not in {
                    "iter", "next", "len", "bool", "hash", "repr", "str", "dict", "list",
                    "tuple", "set", "frozenset", "isinstance", "issubclass", "type", "super",
                    "getattr", "setattr", "delattr", "hasattr",
                } or not hasattr(builtins, node.func.id):
                    raise ValueError("external receiver escapes to an opaque helper")
        return names

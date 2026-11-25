from __future__ import annotations

import ast
import builtins
import inspect
import re
import typing
from abc import ABCMeta
from importlib import import_module
from textwrap import dedent
from types import FunctionType, GetSetDescriptorType, MemberDescriptorType, UnionType
from typing import TYPE_CHECKING, cast, get_args, get_origin, is_typeddict

from ..annotations import TypeHint, merge_hints

if TYPE_CHECKING:
    from ..analysis import Binding
    from .allowlist import AllowlistRenamer

__all__: list[str] = []


class ExternalReturns:
    """Read external return annotations without calling APIs or evaluating hints."""

    def __init__(self, analysis: AllowlistRenamer) -> None:
        self.analysis: AllowlistRenamer = analysis
        self.cache: dict[ast.expr, TypeHint] = {}
        self.annotations: dict[object, ast.expr | None] = {}
        self.paths: dict[str, TypeHint] = {}

    def resolve(self, path: str) -> object:
        parts = path.split(".")
        for index in range(len(parts) - 1, 0, -1):
            try:
                value: object = import_module(".".join(parts[:index]))
            except ImportError:
                continue
            for name in parts[index:]:
                value = inspect.getattr_static(value, name)
            return value
        raise ValueError("unresolved external annotation")

    def imported(self, node: ast.expr) -> object | None:
        if isinstance(node, ast.Attribute):
            owner = self.imported(node.value)
            return inspect.getattr_static(owner, node.attr, None) if owner is not None else None
        if not isinstance(node, ast.Name):
            return None
        binding = self.analysis.bindings.get(node)
        definitions = self.analysis.definitions(binding) if binding is not None else []
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            return None
        module, name = self.analysis.imports.get(definitions[0], ("", None))
        if not module:
            return None
        imported = import_module(module)
        return inspect.getattr_static(imported, name, None) if name is not None else imported

    def annotation(self, node: ast.expr, namespace: dict[str, object], parameters: dict[str, TypeHint], receiver: TypeHint) -> TypeHint:
        if isinstance(node, ast.Constant) and node.value is None:
            return TypeHint(("types.NoneType",))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.annotation(ast.parse(node.value, mode="eval").body, namespace, parameters, receiver)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = self.annotation(node.left, namespace, parameters, receiver)
            right = self.annotation(node.right, namespace, parameters, receiver)
            return merge_hints(left, right) if left and right else TypeHint()
        if isinstance(node, ast.Name):
            if node.id in parameters:
                return parameters[node.id]
            value = namespace.get(node.id, vars(builtins).get(node.id))
        elif isinstance(node, ast.Attribute):
            parts = ast.unparse(node).split(".")
            value = namespace.get(parts[0])
            for name in parts[1:]:
                value = inspect.getattr_static(value, name, None)
        elif isinstance(node, ast.Subscript):
            base = self.annotation(node.value, namespace, parameters, receiver)
            elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            arguments = tuple(self.annotation(element, namespace, parameters, receiver) for element in elements)
            name = ast.unparse(node.value).rsplit(".", 1)[-1]
            if name in {"Awaitable", "Coroutine", "Future", "Task"}:
                return TypeHint(origin="awaitable", arguments=arguments[-1:])
            if name in {"Optional", "Union"}:
                return merge_hints(*arguments) if all(arguments) else TypeHint()
            return TypeHint(base.classes, arguments=arguments)
        else:
            return TypeHint()
        if value is vars(import_module("typing"))["Self"]:
            return receiver
        return self.runtime_hint(value)

    def runtime_hint(self, value: object) -> TypeHint:
        if value is typing.Any:
            return TypeHint()
        origin = get_origin(value)
        if origin is UnionType:
            arguments = tuple(self.runtime_hint(argument) for argument in get_args(value))
            return merge_hints(*arguments) if all(arguments) else TypeHint()
        if is_typeddict(origin or value):
            # TypedDict classes are not runtime instance types. Their values,
            # including those returned by dict.copy(), are dictionaries.
            return TypeHint(("builtins.dict",))
        arguments = tuple(self.runtime_hint(argument) for argument in get_args(value))
        value = origin or value
        if isinstance(value, type):
            return TypeHint((f"{value.__module__}.{value.__qualname__}",), arguments=arguments)
        return TypeHint()

    def returned(self, function: object, receiver: TypeHint | None = None, owner: type | None = None) -> TypeHint:
        receiver = receiver if receiver is not None else TypeHint()
        if isinstance(function, (staticmethod, classmethod)):
            function = cast(
                classmethod[object, ..., object] | staticmethod[..., object], function
            ).__func__
        if not isinstance(function, FunctionType):
            return TypeHint()
        if function not in self.annotations:
            tree = ast.parse(dedent(inspect.getsource(function)))
            definition = tree.body[0]
            self.annotations[function] = definition.returns if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        annotation = self.annotations[function]
        if annotation is None:
            return TypeHint()
        parameters: dict[str, TypeHint] = {}
        if owner is not None:
            parameters = {
                parameter.__name__: argument
                for parameter, argument in zip(getattr(owner, "__parameters__", ()), receiver.arguments, strict=False)
            }
        hint = self.annotation(annotation, vars(import_module(function.__module__)), parameters, receiver)
        if inspect.iscoroutinefunction(function):
            return TypeHint(origin="awaitable", arguments=(hint,))
        return hint

    def method(self, receiver: TypeHint, name: str) -> TypeHint:
        if receiver.self_reference or receiver.incomplete:
            return TypeHint()
        if name == "copy" and receiver.classes == ("builtins.dict",):
            return receiver
        hints: list[TypeHint] = []
        for path in receiver.classes:
            owner = self.resolve(path)
            if not isinstance(owner, type):
                return TypeHint()
            hints.append(self.returned(inspect.getattr_static(owner, name, None), receiver, owner))
        return merge_hints(*hints) if all(hints) else TypeHint()

    def hint(self, node: ast.expr, seen: frozenset[ast.AST] = frozenset()) -> TypeHint:
        if node in seen:
            return TypeHint()
        if not seen and node in self.cache:
            return self.cache[node]
        try:
            result = self.expression(node, seen | {node})
        except (ImportError, AttributeError, ValueError, TypeError, OSError, SyntaxError):
            result = TypeHint()
        if not seen:
            self.cache[node] = result
        return result

    def normalize(self, hint: TypeHint) -> TypeHint:
        """Expand imported alias objects without evaluating annotation expressions."""
        if hint.incomplete:
            return TypeHint()
        if not hint.classes or hint.origin is not None:
            return hint
        hints: list[TypeHint] = []
        for path in hint.classes:
            if path in self.analysis.annotation_classes:
                hints.append(TypeHint((path,)))
                continue
            if path not in self.paths:
                try:
                    self.paths[path] = self.runtime_hint(self.resolve(path))
                except (ImportError, AttributeError, ValueError):
                    self.paths[path] = TypeHint()
            hints.append(self.paths[path])
        if not all(hints):
            return TypeHint()
        result = merge_hints(*hints)
        return TypeHint(
            result.classes, hint.self_reference, arguments=hint.arguments or result.arguments
        )

    def reaching_local(
        self, node: ast.Name, binding: Binding, definitions: list[ast.AST]
    ) -> ast.AST | None:
        """Find the reaching definition for a straight-line local rebinding."""
        child: ast.AST = node
        parent = self.analysis.parents.get(child)
        while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(parent, (ast.ClassDef, ast.Lambda)):
                return None
            child, parent = parent, self.analysis.parents.get(parent)
        if (
            parent is None
            or not isinstance(child, ast.stmt)
            or child not in parent.body
            or binding not in self.analysis.node_bindings(parent)
        ):
            return None
        if any(isinstance(ref, (ast.Global, ast.Nonlocal)) for ref in binding.references):
            return None
        positions = {statement: index for index, statement in enumerate(parent.body)}
        use = positions[child]
        reaching: dict[int, ast.AST] = {}
        for definition in definitions:
            if isinstance(definition, ast.arg) and self.analysis.parents.get(definition) is parent.args:
                reaching[-1] = definition
                continue
            assignment = self.analysis.parents.get(definition)
            if not (
                isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and assignment in positions
                and assignment.value is not None
                and isinstance(definition, ast.Name)
                and (
                    assignment.targets == [definition]
                    if isinstance(assignment, ast.Assign)
                    else assignment.target is definition
                )
            ):
                return None
            if positions[assignment] < use:
                reaching[positions[assignment]] = definition
        return reaching[max(reaching)] if reaching else None

    def expression(self, node: ast.expr, seen: frozenset[ast.AST]) -> TypeHint:
        if not isinstance(node, (ast.Name, ast.IfExp)):
            hint = self.analysis.value_hint(node)
            if hint:
                return self.normalize(hint)
        if isinstance(node, ast.Await):
            awaited = self.hint(node.value, seen)
            if awaited.origin == "awaitable":
                return awaited.element()
            # Custom awaitables must supply their own annotated protocol result.
            protocol = self.method(awaited, "__await__")
            return protocol.element() if protocol.origin == "awaitable" else TypeHint()
        if isinstance(node, ast.NamedExpr):
            return self.hint(node.value, seen)
        if isinstance(node, ast.Call):
            imported = self.imported(node.func)
            if isinstance(imported, type):
                return TypeHint((f"{imported.__module__}.{imported.__qualname__}",))
            if imported is not None:
                return self.returned(imported)
            if isinstance(node.func, ast.Attribute):
                return self.method(self.hint(node.func.value, seen), node.func.attr)
        if isinstance(node, ast.Name):
            binding = self.analysis.bindings.get(node)
            definitions = self.analysis.definitions(binding) if binding is not None else []
            if len(definitions) > 1 and isinstance(node.ctx, ast.Load):
                if binding is not None:
                    reaching = self.reaching_local(node, binding, definitions)
                    if reaching is not None:
                        definitions = [reaching]
                # A later, separate ``with ... as name`` does not change the
                # receiver inside this context's body. Only use this lexical
                # evidence when no competing store occurs anywhere in the body.
                child: ast.AST = node
                parent = self.analysis.parents.get(child)
                while parent is not None:
                    if isinstance(parent, (ast.With, ast.AsyncWith)) and child in parent.body:
                        candidates = [
                            definition for definition in definitions
                            if any(item.optional_vars is definition for item in parent.items)
                        ]
                        body_nodes = {descendant for statement in parent.body for descendant in ast.walk(statement)}
                        if len(candidates) == 1 and not any(definition in body_nodes for definition in definitions):
                            definitions = candidates
                            break
                    child = parent
                    parent = self.analysis.parents.get(child)
            if len(definitions) != 1:
                return TypeHint()
            definition = definitions[0]
            annotation = definition.__dict__.get("type_annotation")
            if isinstance(annotation, TypeHint):
                return self.normalize(annotation)
            parent = self.analysis.parents.get(definition)
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and parent.value is not None:
                return self.hint(parent.value, seen)
            if isinstance(parent, ast.withitem):
                statement = self.analysis.parents.get(parent)
                asynchronous = isinstance(statement, ast.AsyncWith)
                entered = self.method(self.hint(parent.context_expr, seen), "__aenter__" if asynchronous else "__enter__")
                return entered.element() if asynchronous and entered.origin == "awaitable" else entered
        if isinstance(node, ast.IfExp):
            left, right = self.hint(node.body, seen), self.hint(node.orelse, seen)
            return merge_hints(left, right) if left and right else TypeHint()
        return TypeHint()


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

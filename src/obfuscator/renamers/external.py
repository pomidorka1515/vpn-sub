from __future__ import annotations

import ast
import builtins
import inspect
import re
import typing
from abc import ABCMeta
from collections import deque
from dataclasses import replace
from importlib import import_module
from textwrap import dedent
from types import (
    BuiltinFunctionType,
    FunctionType,
    GetSetDescriptorType,
    MemberDescriptorType,
    ModuleType,
    UnionType,
)
from typing import TYPE_CHECKING, cast, get_args, get_origin, is_typeddict

from ..annotations import AnnotationIndex, TypeHint, container_origin, merge_hints
from ..inference import operation_hint
from .external_source import source_return
from .stub_returns import StubReturns

if TYPE_CHECKING:
    from ..analysis import Binding
    from .allowlist import AllowlistRenamer

__all__: list[str] = []

_MISSING: object = object()
_MODULE_CLASS_NAMES: frozenset[str] = frozenset(
    name for base in ModuleType.__mro__ for name in vars(base)
)


def _static_member(owner: object, name: str, default: object = _MISSING) -> object:
    """Read own namespaces directly when static lookup has no precedence rules to apply."""
    if type(owner) is type or (type(owner) is ModuleType and name not in _MODULE_CLASS_NAMES):
        # For an ordinary class, the first namespace is its mappingproxy;
        # return the raw member, never an invoked descriptor. An exact module
        # has a plain dict, but class attributes can take precedence over it.
        value = vars(owner).get(name, _MISSING)
        if value is not _MISSING:
            return value
    if default is _MISSING:
        return inspect.getattr_static(owner, name)
    return inspect.getattr_static(owner, name, default)


def _complete_hint(hint: TypeHint) -> bool:
    return bool(hint) and not hint.incomplete and (
        bool(hint.classes) or all(_complete_hint(argument) for argument in hint.arguments)
    )


class ExternalReturns:
    """Read external return annotations without calling APIs or evaluating hints."""

    def __init__(self, analysis: AllowlistRenamer) -> None:
        self.analysis: AllowlistRenamer = analysis
        self.cache: dict[ast.expr, TypeHint] = {}
        self.annotations: dict[object, ast.expr | None] = {}
        self.implementations: dict[FunctionType, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.active_returns: set[FunctionType] = set()
        self.stubs: StubReturns = StubReturns()
        self.paths: dict[str, TypeHint] = {}
        self.objects: dict[str, object] = {}
        self.sources: dict[str, tuple[AnnotationIndex, dict[str, ast.expr | ast.ClassDef | None]]] = {}
        self.statement_positions: dict[ast.FunctionDef | ast.AsyncFunctionDef, dict[ast.stmt, int]] = {}

    def resolve(self, path: str) -> object:
        if path in self.objects:
            return self.objects[path]
        parts = path.split(".")
        for index in range(len(parts) - 1, 0, -1):
            try:
                value: object = import_module(".".join(parts[:index]))
            except ImportError:
                continue
            for name in parts[index:]:
                value = _static_member(value, name)
            self.objects[path] = value
            return value
        raise ValueError("unresolved external annotation")

    def imported(self, node: ast.expr) -> object | None:
        if isinstance(node, ast.Attribute):
            owner = self.imported(node.value)
            return _static_member(owner, node.attr, None) if owner is not None else None
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
        return _static_member(imported, name, None) if name is not None else imported

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
                value = _static_member(value, name, None)
        elif isinstance(node, ast.Subscript):
            base = self.annotation(node.value, namespace, parameters, receiver)
            elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            arguments = tuple(self.annotation(element, namespace, parameters, receiver) for element in elements)
            name = ast.unparse(node.value).rsplit(".", 1)[-1]
            if name in {"Awaitable", "Coroutine", "Future", "Task"}:
                return TypeHint(origin="awaitable", arguments=arguments[-1:])
            if name in {"Optional", "Union"}:
                return merge_hints(*arguments) if all(arguments) else TypeHint()
            variadic = base.origin == "tuple" and bool(elements) and isinstance(elements[-1], ast.Constant) and elements[-1].value is Ellipsis
            return TypeHint(base.classes, origin=base.origin, arguments=arguments[:-1] if variadic else arguments, variadic=variadic)
        else:
            return TypeHint()
        if value is typing.Self:
            return receiver
        return self.runtime_hint(value)

    def runtime_hint(self, value: object) -> TypeHint:
        if value is typing.Any:
            return TypeHint()
        if value is type(None):
            # NoneType reports builtins as its module but is exported by types.
            return TypeHint(("types.NoneType",))
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
            path = f"{value.__module__}.{value.__qualname__}"
            return TypeHint((path,), origin=container_origin(path), arguments=arguments)
        return TypeHint()

    def returned(self, function: object, receiver: TypeHint | None = None, owner: type | None = None, *, call: ast.Call | None = None) -> TypeHint:
        receiver = receiver if receiver is not None else TypeHint()
        if isinstance(function, BuiltinFunctionType):
            return self.stubs.returned(f"{function.__module__}.{function.__name__}", call)
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
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.implementations[function] = definition
        annotation = self.annotations[function]
        if annotation is None:
            implementation = self.implementations.get(function)
            if implementation is None or function in self.active_returns:
                return TypeHint()
            self.active_returns.add(function)
            try:
                hint = source_return(self, function, implementation, receiver)
            finally:
                self.active_returns.remove(function)
            return TypeHint(origin="awaitable", arguments=(hint,)) if inspect.iscoroutinefunction(function) else hint
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

    def method(self, receiver: TypeHint, name: str, *, call: ast.Call | None = None) -> TypeHint:
        if receiver.self_reference or receiver.incomplete:
            return TypeHint()
        if name == "copy" and receiver.classes == ("builtins.dict",):
            return receiver
        hints: list[TypeHint] = []
        for path in receiver.classes:
            owner = self.resolve(path)
            if not isinstance(owner, type):
                return TypeHint()
            member = _static_member(owner, name, None)
            stub = self.stubs.method(owner, name, call, receiver, overloaded_only=isinstance(member, FunctionType)) if member is not None else TypeHint()
            hints.append(stub or self.returned(member, receiver, owner, call=call))
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

    def declared(self, path: str, seen: frozenset[str] = frozenset()) -> TypeHint:
        """Read an imported global's declared type, never its proxied value."""
        if path in seen:
            return TypeHint()
        seen |= {path}
        module_name, _, name = path.rpartition(".")
        if module_name not in self.sources:
            module = import_module(module_name)
            tree = ast.parse(inspect.getsource(module))
            index = AnnotationIndex(module_name, tree, is_package=hasattr(module, "__path__"))
            declarations: dict[str, ast.expr | ast.ClassDef | None] = {}
            pending = list(tree.body)
            while pending:
                statement = pending.pop()
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    declarations[statement.target.id] = statement.annotation if statement.target.id not in declarations else None
                elif isinstance(statement, ast.ClassDef):
                    declarations[statement.name] = statement if statement.name not in declarations else None
                elif isinstance(statement, ast.If):
                    pending.extend((*statement.body, *statement.orelse))
                elif isinstance(statement, ast.Try):
                    pending.extend((*statement.body, *statement.orelse, *statement.finalbody))
                    for handler in statement.handlers:
                        pending.extend(handler.body)
            self.sources[module_name] = index, declarations
        index, declarations = self.sources[module_name]
        declaration = declarations.get(name)
        if isinstance(declaration, ast.ClassDef):
            if declaration.decorator_list or declaration.keywords:
                return TypeHint()
            bases = [index.hint(base.value if isinstance(base, ast.Subscript) else base) for base in declaration.bases]
            if any(base.origin == "protocol" for base in bases):
                return TypeHint(origin="protocol")
            parents = [self.normalize(base, seen) for base in bases]
            if not parents or not all(parents):
                return TypeHint()
            concrete = [parent for parent in parents if parent.origin != "protocol"]
            return merge_hints(*concrete)
        if declaration is not None:
            return self.normalize(index.hint(declaration), seen)
        if name in declarations:
            return TypeHint()
        target = index.names.get(name)
        return self.declared(target, seen) if target is not None else TypeHint()

    def normalize(self, hint: TypeHint, seen: frozenset[str] = frozenset()) -> TypeHint:
        """Expand imported alias objects without evaluating annotation expressions."""
        if hint.incomplete:
            return TypeHint()
        if not hint.classes or hint.origin is not None:
            return hint
        hints: list[TypeHint] = []
        aliases: dict[str, TypeHint] = self.analysis.tree.__dict__.get("annotation_aliases", {})
        for path in hint.classes:
            if path in aliases:
                if path in seen:
                    return TypeHint()
                hints.append(self.normalize(aliases[path], seen | {path}))
                continue
            if path in self.analysis.annotation_classes:
                hints.append(TypeHint((path,)))
                continue
            if path not in self.paths:
                try:
                    self.paths[path] = self.runtime_hint(self.resolve(path))
                except (ImportError, AttributeError, ValueError):
                    self.paths[path] = self.declared(path, seen)
            hints.append(self.paths[path])
        if not all(hints):
            return TypeHint()
        result = merge_hints(*hints)
        return TypeHint(
            result.classes, hint.self_reference, origin=result.origin,
            arguments=hint.arguments or result.arguments, variadic=result.variadic,
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
        if parent is None or not isinstance(child, ast.stmt):
            return None
        positions = self.statement_positions.get(parent)
        if positions is None:
            positions = {statement: index for index, statement in enumerate(parent.body)}
            self.statement_positions[parent] = positions
        if child not in positions or binding not in self.analysis.node_bindings(parent):
            return None
        if any(isinstance(ref, (ast.Global, ast.Nonlocal)) for ref in binding.references):
            return None
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

    def external_factory(self, call: ast.Call, seen: frozenset[ast.AST]) -> bool:
        """Verify an external callable binding without invoking its implementation."""
        if callable(self.imported(call.func)):
            return True
        if not isinstance(call.func, ast.Attribute):
            return False
        receiver = self.hint(call.func.value, seen)
        if not _complete_hint(receiver) or not receiver.classes or receiver.self_reference:
            return False
        for path in receiver.classes:
            if path in self.analysis.annotation_classes:
                return False
            owner = self.resolve(path)
            if not isinstance(owner, type) or not callable(_static_member(owner, call.func.attr, None)):
                return False
        return True

    def rebound_hint(self, definitions: list[ast.AST], seen: frozenset[ast.AST]) -> TypeHint:
        """Retain declared external factory contracts across cleanup-path stores."""
        declarations = [
            hint for definition in definitions
            if isinstance(hint := definition.__dict__.get("type_annotation"), TypeHint)
        ]
        if not declarations:
            return TypeHint()
        declared = self.normalize(merge_hints(*declarations))
        if not _complete_hint(declared):
            return TypeHint()
        alternatives = [declared]
        for definition in definitions:
            if isinstance(definition, ast.arg):
                if not isinstance(definition.__dict__.get("type_annotation"), TypeHint):
                    return TypeHint()
                continue
            assignment = self.analysis.parents.get(definition)
            if not isinstance(definition, ast.Name) or not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                return TypeHint()
            value = assignment.value
            if value is None or (isinstance(value, ast.Constant) and value.value is None):
                continue
            inferred = self.hint(value, seen)
            if _complete_hint(inferred):
                alternatives.append(inferred)
            elif not isinstance(value, ast.Call) or not self.external_factory(value, seen):
                return TypeHint()
        return merge_hints(*alternatives)

    def expression(self, node: ast.expr, seen: frozenset[ast.AST]) -> TypeHint:
        if isinstance(node.__dict__.get("expression_type"), TypeHint):
            return self.normalize(self.analysis.value_hint(node))
        if not isinstance(node, (ast.Name, ast.IfExp, ast.Await)):
            hint = self.analysis.value_hint(node)
            if _complete_hint(hint):
                return self.normalize(hint)
        if isinstance(node, ast.Await):
            awaited = self.hint(node.value, seen)
            if awaited.origin == "awaitable":
                return awaited.element()
            # Custom awaitables must supply their own annotated protocol result.
            if awaited.incomplete or awaited.self_reference or not awaited.classes:
                return TypeHint()
            results: list[TypeHint] = []
            for awaited_path in awaited.classes:
                protocol = self.method(replace(awaited, classes=(awaited_path,)), "__await__")
                if protocol.origin == "awaitable":
                    result = protocol.element()
                # A Generator's third argument is its return value, not a
                # value yielded while driving the iterator.
                elif protocol.classes == ("collections.abc.Generator",) and len(protocol.arguments) == 3 and not protocol.incomplete:
                    result = protocol.arguments[2]
                else:
                    return TypeHint()
                if not result or result.incomplete:
                    return TypeHint()
                results.append(result)
            return merge_hints(*results)
        if isinstance(node, ast.NamedExpr):
            return self.hint(node.value, seen)
        if isinstance(node, ast.Attribute):
            owners = self.analysis.receiver(node.value)[0]
            hints: list[TypeHint] = []
            for owner in owners:
                values: list[TypeHint] = []
                for base in self.analysis.class_mro(owner, set()):
                    values.extend(
                        self.hint(assignment.value, seen)
                        for assignment in self.analysis.instance_assignments.get((id(base), node.attr), [])
                        if assignment.value is not None
                    )
                    binding = self.analysis.member_binding(base, node.attr)
                    for definition in self.analysis.definitions(binding) if binding is not None else []:
                        parent = self.analysis.parents.get(definition)
                        if isinstance(definition, ast.Name) and isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                            values.append(self.hint(parent.value, seen))
                hints.append(merge_hints(*values) if values and all(values) else TypeHint())
            return merge_hints(*hints) if hints and all(hints) else TypeHint()
        if isinstance(node, ast.Call):
            structural = operation_hint(node, self.analysis.imported_path(node.func), lambda expression: self.hint(expression, seen) if isinstance(expression, ast.expr) else TypeHint())
            if structural is not None:
                return structural
            imported = self.imported(node.func)
            if isinstance(imported, type):
                return TypeHint((f"{imported.__module__}.{imported.__qualname__}",))
            if imported is not None:
                return self.returned(imported, call=node)
            if isinstance(node.func, ast.Attribute):
                return self.method(self.hint(node.func.value, seen), node.func.attr, call=node)
        if isinstance(node, ast.Name):
            binding = self.analysis.bindings.get(node)
            definitions: list[ast.AST] = self.analysis.definitions(binding) if binding is not None else []
            # Exception targets are definitions themselves (not Store Names).
            # A sibling handler's binding is not live in this handler. Do not
            # narrow past a competing assignment, closure, or except* group.
            if len(definitions) > 1 and any(isinstance(definition, ast.ExceptHandler) for definition in definitions):
                handler_child: ast.AST = node
                ancestor = self.analysis.parents.get(handler_child)
                while ancestor is not None:
                    if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                        break
                    if isinstance(ancestor, ast.ExceptHandler) and handler_child in ancestor.body:
                        if ancestor in definitions and not any(
                            definition is not ancestor and self.in_context_body(definition, ancestor)
                            for definition in definitions
                        ):
                            definitions = [ancestor]
                            break
                    handler_child, ancestor = ancestor, self.analysis.parents.get(ancestor)
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
                        if len(candidates) == 1 and not any(
                            self.in_context_body(definition, parent) for definition in definitions
                        ):
                            definitions = candidates
                            break
                    child = parent
                    parent = self.analysis.parents.get(child)
            if len(definitions) != 1:
                declared = self.analysis.value_hint(node)
                normalized = self.normalize(declared) if declared else TypeHint()
                return normalized or self.rebound_hint(definitions, seen)
            definition = definitions[0]
            if isinstance(definition, ast.ExceptHandler):
                if isinstance(self.analysis.parents.get(definition), ast.TryStar):
                    return TypeHint()
                caught = definition.type
                alternatives = caught.elts if isinstance(caught, ast.Tuple) else [caught]
                exception_hints: list[TypeHint] = []
                for alternative in alternatives:
                    if alternative is None:
                        return TypeHint()
                    path = self.analysis.imported_path(alternative)
                    if path is None:
                        return TypeHint()
                    exception = self.resolve(path)
                    if not isinstance(exception, type) or not issubclass(exception, BaseException):
                        return TypeHint()
                    exception_hints.append(self.runtime_hint(exception))
                return merge_hints(*exception_hints) if exception_hints and all(exception_hints) else TypeHint()
            if isinstance(definition, ast.alias):
                path = self.analysis.imported_path(node)
                return self.declared(path) if path is not None else TypeHint()
            annotation = definition.__dict__.get("type_annotation")
            if isinstance(annotation, TypeHint):
                return self.normalize(annotation)
            parent = self.analysis.parents.get(definition)
            if isinstance(parent, (ast.For, ast.AsyncFor, ast.comprehension)) and parent.target is definition:
                return self.normalize(self.hint(parent.iter, seen).iterated())
            if isinstance(parent, (ast.Tuple, ast.List)):
                located = self.analysis.target_indices(definition)
                if located is not None:
                    indices, container = located
                    statement = self.analysis.parents.get(container)
                    if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                        hint = self.hint(statement.value, seen)
                    elif isinstance(statement, (ast.For, ast.AsyncFor, ast.comprehension)):
                        hint = self.hint(statement.iter, seen).iterated()
                    else:
                        return TypeHint()
                    for index in indices:
                        hint = hint.element(index)
                    return hint
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and parent.value is not None:
                return self.hint(parent.value, seen)
            if isinstance(parent, ast.withitem):
                statement = self.analysis.parents.get(parent)
                asynchronous = isinstance(statement, ast.AsyncWith)
                context = self.hint(parent.context_expr, seen)
                if context.origin == ("async_contextmanager" if asynchronous else "contextmanager"):
                    return context.element()
                entered = self.method(context, "__aenter__" if asynchronous else "__enter__")
                return entered.element() if asynchronous and entered.origin == "awaitable" else entered
        if isinstance(node, ast.IfExp):
            left, right = self.hint(node.body, seen), self.hint(node.orelse, seen)
            return merge_hints(left, right) if left and right else TypeHint()
        return TypeHint()

    def in_context_body(self, node: ast.AST, context: ast.With | ast.AsyncWith | ast.ExceptHandler) -> bool:
        # Walk from the handful of competing stores, not through every
        # descendant of the with suite for each receiver use.
        parent = self.analysis.parents.get(node)
        while parent is not None:
            if parent is context:
                return node in context.body
            node, parent = parent, self.analysis.parents.get(parent)
        return False


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
            value = _static_member(value, name)
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
        nodes = list(ast.walk(tree))
        locals_ = {
            node.arg if isinstance(node, ast.arg) else node.id
            for node in nodes
            if isinstance(node, ast.arg) or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        }
        receivers = {"self", "cls"}
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = node.args.posonlyargs + node.args.args
                if positional:
                    receivers.add(positional[0].arg)
        # Solve alias reachability once rather than rescanning every AST node
        # for each link in a chain. Cycles terminate at the visited-name set.
        aliases: dict[str, list[str]] = {}
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.setdefault(node.value.id, []).extend(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
        pending = deque(receivers)
        while pending:
            for name in aliases.get(pending.popleft(), ()):
                if name not in receivers:
                    receivers.add(name)
                    pending.append(name)
        for node in nodes:
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
                    # Explicit base calls (Base.__init__(self)) have the same
                    # inspectable implementation as a directly named helper.
                    reference = node.func
                    path: list[str] = []
                    while isinstance(reference, ast.Attribute):
                        path.insert(0, reference.attr)
                        reference = reference.value
                    if isinstance(reference, ast.Name) and reference.id not in locals_:
                        qualified = vars(module).get(reference.id)
                        for part in path:
                            qualified = _static_member(qualified, part, None)
                        if isinstance(qualified, FunctionType):
                            names.update(self.implementation_members(qualified, seen))
                            continue
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

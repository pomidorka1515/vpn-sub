from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from types import FunctionType
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from .external import ExternalReturns
from .external_usage import ExternalUsage

if TYPE_CHECKING:
    from ..analysis import Binding, Function
    from .allowlist import AllowlistRenamer

__all__: list[str] = []


@dataclass(frozen=True, slots=True)
class CallableValue:
    function: Function
    bound: bool = False
    receivers: frozenset[ast.ClassDef] = frozenset()


@dataclass(frozen=True, slots=True)
class Value:
    owners: frozenset[ast.ClassDef] = frozenset()
    functions: frozenset[CallableValue] = frozenset()
    unknown: bool = False
    containers: frozenset[ast.expr] = frozenset()
    strings: frozenset[str] = frozenset()

    def merge(self, other: Value) -> Value:
        if other is _BOTTOM or self is other:
            return self
        if self is _BOTTOM:
            return other
        if (other.owners.issubset(self.owners) and other.functions.issubset(self.functions)
                and other.containers.issubset(self.containers) and other.strings.issubset(self.strings)
                and (self.unknown or not other.unknown)):
            return self
        if (self.owners.issubset(other.owners) and self.functions.issubset(other.functions)
                and self.containers.issubset(other.containers) and self.strings.issubset(other.strings)
                and (other.unknown or not self.unknown)):
            return other
        return Value(self.owners | other.owners, self.functions | other.functions, self.unknown or other.unknown,
                     self.containers | other.containers, self.strings | other.strings)


_BOTTOM: Value = Value()
_UNKNOWN: Value = Value(unknown=True)


class AttributeFlow:
    """Monotone, whole-program receiver/callback flow for opt-in field renames.

    An annotation can supply receiver information, but never proves a callback's
    implementation. Every incoming callable value must resolve to visible code.
    Joins retain unknown alternatives; recursion converges without executing code.
    """

    def __init__(self, analysis: AllowlistRenamer) -> None:
        self.analysis: AllowlistRenamer = analysis
        self.nodes: list[ast.AST] = [node for node in analysis.nodes if id(node) not in analysis.annotation_nodes]
        self.arguments: dict[Binding, Value] = {}
        self.fields: dict[tuple[ast.ClassDef, str], Value] = {}
        self.external_types: dict[str, type | None] = {}
        self.external_returns: ExternalReturns = ExternalReturns(analysis)
        self.external_usage: ExternalUsage = ExternalUsage()
        self.values: dict[ast.AST, Value] = {}
        self.static_receivers: dict[ast.expr, frozenset[ast.ClassDef]] = {}
        self.static_targets: dict[ast.expr, ast.AST | str | None] = {}
        self.local_forwarders: dict[ast.FunctionDef, bool] = {}
        self.argument_bindings: dict[tuple[ast.Call, CallableValue], dict[ast.arg, ast.expr] | None] = {}
        self.reaching: dict[ast.Name, list[ast.AST]] = {}
        self.records: dict[ast.ClassDef, tuple[bool, dict[str, ast.expr | None]]] = {}
        self.container_items: dict[ast.expr, Value] = {}
        self.container_values: dict[ast.expr, Value] = {}
        self.mapping_views: set[ast.Call] = set()
        self.zipped: dict[ast.Call, ast.Tuple] = {}
        self.sequence_sources: dict[ast.Tuple, tuple[ast.expr | None, ...]] = {}
        self.keyword_sources: dict[ast.Dict, tuple[tuple[str | None, ast.expr], ...]] = {}
        self.positional_sources: dict[ast.Tuple, tuple[ast.expr, ...]] = {}
        self.positional_packs: dict[tuple[ast.Call, CallableValue], ast.Tuple] = {}
        self.keyword_packs: dict[tuple[ast.Call, CallableValue], ast.Dict] = {}
        self.projections: dict[tuple[ast.expr, int], ast.Subscript] = {}
        self.opaque_callbacks: set[Function] = set()
        self.context_covered: dict[frozenset[ast.ClassDef], set[ast.Call]] = {}
        self.context_active: set[Function] = set()
        self.function_calls: dict[Function, list[ast.Call]] = {}
        self.external_containers: dict[str, ast.expr] = {}
        self.external_container_owners: dict[ast.expr, type] = {}
        for node in self.nodes:
            if isinstance(node, ast.Call):
                binding = analysis.bindings.get(node.func)
                if (isinstance(cast(object, binding), BuiltinBinding) and getattr(binding, "name", "") in {"zip", "enumerate"}
                        and not any(isinstance(argument, ast.Starred) for argument in node.args)):
                    pair = ast.Tuple(elts=[], ctx=ast.Load())
                    self.zipped[node] = pair
                    self.sequence_sources[pair] = tuple(node.args) if getattr(binding, "name", "") == "zip" else (None, *node.args[:1])
                    self.container_items[node] = Value(containers=frozenset({pair}))
                    self.container_items[pair] = _BOTTOM
            if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.ListComp, ast.SetComp, ast.GeneratorExp)) and not isinstance(getattr(node, "ctx", None), ast.Store):
                self.container_items[node] = _BOTTOM
            elif isinstance(node, ast.Dict):
                self.container_items[node] = _BOTTOM
                self.container_values[node] = _BOTTOM
            if isinstance(node, ast.Call) and len(node.args) <= 1 and not node.keywords:
                binding = analysis.bindings.get(node.func)
                if (isinstance(cast(object, binding), BuiltinBinding) and getattr(binding, "name", "") in {"set", "list", "tuple", "frozenset", "iter", "reversed"}) or (
                    not node.args and analysis.imported_path(node.func) == "weakref.WeakSet"
                ):
                    self.container_items[node] = _BOTTOM
                elif not node.args and ((isinstance(cast(object, binding), BuiltinBinding) and getattr(binding, "name", "") == "dict") or (
                    analysis.imported_path(node.func) in {"weakref.WeakKeyDictionary", "collections.OrderedDict"})
                ):
                    self.container_items[node] = _BOTTOM
                    self.container_values[node] = _BOTTOM
        inspected_paths: set[str] = set()
        for node in self.nodes:
            candidate = node.value if isinstance(node, ast.Subscript) else (
                node.func.value if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
            )
            if candidate is None:
                continue
            path = analysis.imported_path(candidate)
            if path is None or path in inspected_paths:
                continue
            inspected_paths.add(path)
            try:
                mapping = self.external_returns.resolve(path)
                if type(mapping) is not dict:
                    continue
                prefix = candidate
                while isinstance(prefix, ast.Attribute):
                    prefix = prefix.value
                    prefix_path = analysis.imported_path(prefix)
                    namespace = self.external_returns.resolve(prefix_path) if prefix_path is not None else None
                    if isinstance(namespace, type):
                        self.external_containers[path] = candidate
                        self.external_container_owners[candidate] = namespace
                        self.container_items[candidate] = _UNKNOWN
                        self.container_values[candidate] = _UNKNOWN
                        break
            except (ImportError, AttributeError, ValueError, TypeError):
                continue
        for owner in analysis.classes:
            if (record := self.record_fields(owner)) is not None:
                self.records[owner] = record
        self.written_fields: set[str] = {
            node.attr for node in self.nodes if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
        }
        self.returns: dict[Function, list[ast.expr]] = {function: [] for function in analysis.functions}
        for node in self.nodes:
            if isinstance(node, ast.Return) and node.value is not None:
                function = self.enclosing_function(node)
                if function is not None:
                    self.returns[function].append(node.value)
            elif isinstance(node, ast.Lambda):
                self.returns[node].append(node.body)
            if isinstance(node, ast.Call) and (function := self.enclosing_function(node)) is not None:
                self.function_calls.setdefault(function, []).append(node)
        self.changed: bool = False
        self.solve()
        # The field graph is stable after solving. Reverse it once so each
        # selected family can find all containers in a single graph walk.
        self.field_containers: dict[ast.ClassDef, set[ast.ClassDef]] = {}
        for (owner, _), field in self.fields.items():
            for contained in self.contained_owners(field):
                self.field_containers.setdefault(contained, set()).add(owner)
        for owner in analysis.classes:
            for binding in analysis.node_bindings(owner):
                for definition in analysis.definitions(binding):
                    parent = analysis.parents.get(definition)
                    if isinstance(definition, ast.Name) and isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                        for contained in self.contained_owners(self.value(parent.value)):
                            self.field_containers.setdefault(contained, set()).add(owner)
        self.exposed_owners: dict[frozenset[ast.ClassDef], set[ast.ClassDef]] = {}

    def record_fields(self, owner: ast.ClassDef) -> tuple[bool, dict[str, ast.expr | None]] | None:
        """Recognize dataclasses with a generated, ordinary field-storing initializer."""
        named_tuple = len(owner.bases) == 1 and self.analysis.imported_path(owner.bases[0]) == "typing.NamedTuple"
        if owner.keywords or (owner.bases and not named_tuple) or len(owner.decorator_list) != (0 if named_tuple else 1):
            return None
        decorator = owner.decorator_list[0] if owner.decorator_list else None
        if not named_tuple and (decorator is None or not self.analysis.dataclass_decorator(decorator)):
            return None
        keyword_only = False
        if isinstance(decorator, ast.Call):
            if decorator.args:
                return None
            for option in decorator.keywords:
                if not isinstance(option.value, ast.Constant) or not isinstance(option.value.value, bool):
                    return None
                if option.arg == "kw_only":
                    keyword_only = option.value.value
                elif (option.arg == "init" and not option.value.value) or option.arg not in {
                    "init", "frozen", "slots", "repr", "eq", "order", "unsafe_hash", "match_args", "weakref_slot",
                }:
                    return None
        fields: dict[str, ast.expr | None] = {}
        for statement in owner.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                if statement.target.id.startswith("__") or any(
                    (isinstance(part, ast.Constant) and isinstance(part.value, str))
                    or self.analysis.imported_path(part) in {"typing.ClassVar", "dataclasses.InitVar", "dataclasses.KW_ONLY"}
                    for part in ast.walk(statement.annotation)
                ):
                    return None
                if statement.value is not None and not isinstance(statement.value, ast.Constant):
                    return None
                fields[statement.target.id] = statement.value
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name in {"__init__", "__new__", "__post_init__", "__setattr__", "__getattr__",
                                      "__getattribute__", "__slots__"}:
                    return None
            elif not (isinstance(statement, ast.Pass) or (
                isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            )):
                return None
        if any(len(self.analysis.definitions(binding)) != 1 for binding in self.analysis.node_bindings(owner)):
            return None
        return keyword_only, fields

    def class_receiver(self, node: ast.expr, seen: frozenset[ast.AST] = frozenset()) -> bool:
        if node in seen:
            return False
        if isinstance(self.static_target(node), ast.ClassDef):
            return True
        if isinstance(node, ast.Call):
            binding = self.analysis.bindings.get(node.func)
            return isinstance(cast(object, binding), BuiltinBinding) and getattr(binding, "name", "") == "type" and len(node.args) == 1
        if isinstance(node, ast.Name):
            binding = self.analysis.bindings.get(node)
            definitions = self.analysis.definitions(binding) if binding is not None else []
            return bool(definitions) and all(
                isinstance(parent := self.analysis.parents.get(definition), (ast.Assign, ast.AnnAssign))
                and parent.value is not None and self.class_receiver(parent.value, seen | {node})
                for definition in definitions
            )
        return False

    def member_value(self, receiver: Value, name: str, class_access: bool, seen: frozenset[ast.AST]) -> Value:
        values: list[Value] = []
        for owner in receiver.owners:
            binding = self.analysis.member(owner, name)
            definitions = self.analysis.definitions(binding) if binding is not None else []
            for definition in definitions:
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if self.analysis.is_property(definition):
                        values.append(_UNKNOWN if class_access else self.result(definition, seen))
                    else:
                        bound = self.analysis.descriptor(definition) == "classmethod" or (
                            not class_access and self.analysis.descriptor(definition) != "staticmethod"
                        )
                        values.append(Value(functions=frozenset({CallableValue(definition, bound, frozenset({owner}) if bound else frozenset())})))
                elif isinstance(definition, ast.Name):
                    parent = self.analysis.parents.get(definition)
                    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                        default = self.value(parent.value, seen)
                        values.append(_UNKNOWN if default.functions else default)
            field = self.fields.get((owner, name)) if not class_access else None
            if field is not None:
                values.append(field)
            elif not definitions:
                if not class_access:
                    values.append(_UNKNOWN)
                else:
                    try:
                        self.analysis.check_receiver(owner, name)
                    except ValueError:
                        values.append(_UNKNOWN)
        return self.join(values).merge(Value(unknown=receiver.unknown))

    def descriptor_binding(self, call: ast.Call, seen: frozenset[ast.AST] = frozenset()) -> Value | None:
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "__get__" or len(call.args) != 2 or call.keywords:
            return None
        function = self.value(call.func.value, seen)
        if function.unknown or not function.functions or function.owners or function.containers:
            return None
        if not all(self.python_function(target.function, seen) for target in function.functions):
            return None
        receiver = self.value(call.args[0], seen)
        return Value(functions=frozenset(
            CallableValue(target.function, True, receiver.owners) for target in function.functions
        ), unknown=receiver.unknown)

    def python_function(self, function: Function, seen: frozenset[ast.AST] = frozenset()) -> bool:
        """Prove decorators retain a Python function before using function.__get__ semantics."""
        if isinstance(function, ast.Lambda) or not function.decorator_list:
            return True
        if function in seen:
            return False
        seen |= {function}
        if self.analysis.descriptor(function) is not None or self.analysis.forwarding_decorators(function):
            return True
        for decorator in function.decorator_list:
            if isinstance(decorator, ast.Call) and self.analysis.imported_path(decorator.func) == "functools.wraps":
                continue
            factory = self.value(decorator, seen)
            if factory.unknown or not factory.functions or factory.owners or factory.containers:
                return False
            for target in factory.functions:
                result = self.result(target.function, seen)
                if result.unknown or not result.functions or result.owners or result.containers:
                    return False
                if not all(self.python_function(value.function, seen) for value in result.functions):
                    return False
        return True

    def record_arguments(self, owner: ast.ClassDef, call: ast.Call) -> None:
        keyword_only, fields = self.records[owner]
        bound: dict[str, ast.expr] = {}
        valid = not (keyword_only and call.args) and len(call.args) <= len(fields)
        valid = valid and not any(isinstance(argument, ast.Starred) for argument in call.args)
        if valid:
            bound.update(zip(fields, call.args, strict=False))
            for keyword in call.keywords:
                if keyword.arg not in fields or keyword.arg in bound:
                    valid = False
                    break
                bound[keyword.arg] = keyword.value
        for name, default in fields.items():
            expression = bound.get(name, default)
            self.add_field(owner, name, self.value(expression) if valid and expression is not None else _UNKNOWN)

    def enclosing_function(self, node: ast.AST) -> Function | None:
        parent = self.analysis.parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return parent
            parent = self.analysis.parents.get(parent)
        return None

    def join(self, values: list[Value]) -> Value:
        result = _BOTTOM
        for value in values:
            result = result.merge(value)
        return result

    def static_receiver(self, node: ast.expr) -> frozenset[ast.ClassDef]:
        if node not in self.static_receivers:
            self.static_receivers[node] = frozenset(self.analysis.receiver(node)[0])
        return self.static_receivers[node]

    def static_target(self, node: ast.expr) -> ast.AST | str | None:
        if node not in self.static_targets:
            self.static_targets[node] = self.analysis.target(node, instances=False)
        return self.static_targets[node]

    def reaching_definitions(self, node: ast.Name, binding: Binding) -> list[ast.AST]:
        """Respect definite local rebindings while retaining conditional alternatives."""
        if node in self.reaching:
            return self.reaching[node]
        definitions = self.analysis.definitions(binding)
        scope = self.enclosing_function(node)
        if len(definitions) < 2 or not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return definitions

        ancestors: set[ast.AST] = set()
        part: ast.AST = node
        while part is not scope:
            ancestors.add(part)
            parent = self.analysis.parents.get(part)
            if parent is None:
                return definitions
            part = parent
        stores: dict[ast.AST, list[ast.AST]] = {}
        for definition in definitions:
            if not isinstance(definition, (ast.arg, ast.Name)) or self.enclosing_function(definition) is not scope:
                return definitions
            if isinstance(definition, ast.Name):
                stores.setdefault(self.analysis.parents[definition], []).append(definition)

        def suite(body: list[ast.stmt], incoming: set[ast.AST]) -> set[ast.AST]:
            reaching = incoming
            for statement in body:
                if statement in ancestors:
                    # RHS reads precede the assignment, including augmented
                    # assignments. Descend into the current branch rather than
                    # mixing in later stores or the other branch's values.
                    if isinstance(statement, ast.If):
                        for branch in (statement.body, statement.orelse):
                            if any(child in ancestors for child in branch):
                                return suite(branch, reaching)
                    elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                        for branch in (statement.body, statement.orelse):
                            if any(child in ancestors for child in branch):
                                return suite(branch, set(definitions))
                    elif isinstance(statement, (ast.Try, ast.TryStar)):
                        if any(child in ancestors for child in statement.body):
                            return suite(statement.body, reaching)
                        return set(definitions)
                    elif not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Return, ast.Raise)):
                        return set(definitions)
                    return reaching
                if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement in stores:
                    reaching = set(stores[statement])
                elif isinstance(statement, ast.If):
                    reaching = suite(statement.body, reaching) | suite(statement.orelse, reaching)
                elif isinstance(statement, (ast.Try, ast.TryStar)):
                    completed = suite(statement.orelse, suite(statement.body, reaching))
                    partial = reaching | {
                        definition for child in statement.body for part in ast.walk(child)
                        for definition in stores.get(part, ())
                    }
                    for handler in statement.handlers:
                        completed |= suite(handler.body, partial)
                    reaching = suite(statement.finalbody, completed)
                elif isinstance(statement, (ast.Return, ast.Raise)):
                    return set()
                elif any(part in stores for part in ast.walk(statement)):
                    # Unsupported control flow keeps every alternative. A later
                    # definite local assignment can still close the binding.
                    reaching = set(definitions)
            return reaching

        resolved = list(suite(scope.body, {definition for definition in definitions if isinstance(definition, ast.arg)}))
        self.reaching[node] = resolved or definitions
        return self.reaching[node]

    def value(self, node: ast.AST, seen: frozenset[ast.AST] = frozenset()) -> Value:
        if node in seen:
            return _BOTTOM
        cached = self.values.get(node)
        if cached is not None:
            return cached
        value = self.expression(node, seen)
        if isinstance(node, ast.expr) and node in self.analysis.parents and not value.owners and not value.functions and not value.containers:
            owners = self.static_receiver(node)
            if owners:
                value = Value(owners, unknown=self.analysis.value_hint(node).incomplete)
        self.values[node] = value
        return value

    def iterated_value(self, value: Value) -> Value:
        result = self.join([self.container_items[item] for item in value.containers])
        return result.merge(Value(unknown=value.unknown or bool(value.owners or value.functions)))

    def sequence_value(self, value: Value, index: int, seen: frozenset[ast.AST]) -> Value:
        values: list[Value] = []
        for container in value.containers:
            if isinstance(container, ast.Tuple) and container in self.sequence_sources:
                sources = self.sequence_sources[container]
                if -len(sources) <= index < len(sources):
                    source = sources[index]
                    values.append(self.iterated_value(self.value(source, seen)) if source is not None else _BOTTOM)
            elif isinstance(container, (ast.Tuple, ast.List)) and not any(isinstance(element, ast.Starred) for element in container.elts) and -len(container.elts) <= index < len(container.elts):
                values.append(self.value(container.elts[index], seen))
            else:
                values.append(self.container_items[container])
        return self.join(values).merge(Value(unknown=value.unknown or bool(value.owners or value.functions)))

    def expression(self, node: ast.AST, seen: frozenset[ast.AST]) -> Value:
        if node in seen:
            return _BOTTOM
        seen |= {node}
        analysis = self.analysis
        if isinstance(node, ast.Call) and node in self.mapping_views and isinstance(node.func, ast.Attribute):
            receiver = self.value(node.func.value, seen)
            return Value(containers=frozenset({node}), unknown=receiver.unknown or bool(receiver.owners or receiver.functions))
        if isinstance(node, (ast.Name, ast.Attribute)):
            imported = analysis.imported_path(node)
            if imported in self.external_containers:
                return Value(containers=frozenset({self.external_containers[imported]}))
        if isinstance(node, ast.expr) and node in self.container_items:
            return Value(containers=frozenset({node}))
        if isinstance(node, ast.Lambda):
            return Value(functions=frozenset({CallableValue(node)}))
        if isinstance(node, ast.Name):
            target = self.static_target(node)
            if isinstance(target, ast.ClassDef):
                return Value(frozenset({target}))
            binding = analysis.bindings.get(node)
            definitions = self.reaching_definitions(node, binding) if binding is not None else []
            values: list[Value] = []
            for definition in definitions:
                if isinstance(definition, ast.arg) and binding is not None:
                    inferred = self.static_receiver(node)
                    values.append(self.arguments.get(binding, _BOTTOM).merge(Value(inferred)))
                elif isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    values.append(Value(functions=frozenset({CallableValue(definition)})))
                elif isinstance(definition, ast.ClassDef):
                    values.append(Value(frozenset({definition})))
                elif isinstance(definition, ast.Name):
                    parent = analysis.parents.get(definition)
                    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                        values.append(self.value(parent.value, seen))
                    elif isinstance(parent, (ast.For, ast.comprehension)) and parent.target is definition:
                        iterable = self.value(parent.iter, seen)
                        if iterable.containers:
                            values.append(self.join([self.container_items[item] for item in iterable.containers]).merge(
                                Value(unknown=iterable.unknown or bool(iterable.owners or iterable.functions))
                            ))
                        else:
                            values.append(_UNKNOWN)
                    elif isinstance(parent, (ast.Tuple, ast.List)):
                        located = analysis.target_indices(definition)
                        if located is not None:
                            indices, target = located
                            statement = analysis.parents.get(target)
                            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                                unpacked = self.value(statement.value, seen)
                            elif isinstance(statement, (ast.For, ast.comprehension)):
                                unpacked = self.iterated_value(self.value(statement.iter, seen))
                            else:
                                unpacked = _UNKNOWN
                            for index in indices:
                                unpacked = self.sequence_value(unpacked, index, seen)
                            values.append(unpacked)
                        else:
                            values.append(_UNKNOWN)
                    else:
                        inferred = self.static_receiver(node)
                        values.append(Value(inferred, unknown=not inferred))
                else:
                    values.append(_UNKNOWN)
            if values:
                return self.join(values)
        if isinstance(node, ast.Attribute):
            try:
                inherited = analysis.super_bindings(node)
            except ValueError:
                return _UNKNOWN
            if inherited is not None:
                methods: set[CallableValue] = set()
                for binding in inherited:
                    definitions = analysis.definitions(binding)
                    if len(definitions) != 1:
                        return _UNKNOWN
                    definition = definitions[0]
                    if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) or analysis.is_property(definition):
                        return _UNKNOWN
                    methods.add(CallableValue(definition, analysis.descriptor(definition) != "staticmethod"))
                return Value(functions=frozenset(methods), unknown=not methods)
            receiver = self.value(node.value, seen)
            owners = receiver.owners
            if not owners and not receiver.functions and not receiver.unknown:
                return _BOTTOM
            values = []
            pending = False
            for owner in owners:
                start = len(values)
                binding = analysis.member(owner, node.attr)
                definitions = analysis.definitions(binding) if binding is not None else []
                for definition in definitions:
                    if isinstance(definition, ast.Name):
                        parent = analysis.parents.get(definition)
                        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                            default = self.value(parent.value, seen)
                            # A function assigned as class data can bind as a
                            # descriptor. Do not infer it as an instance field.
                            values.append(_UNKNOWN if default.functions else default)
                    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if analysis.is_property(definition):
                            values.append(self.result(definition, seen))
                        else:
                            class_access = self.static_target(node.value) is owner
                            bound = analysis.descriptor(definition) == "classmethod" or (
                                not class_access and analysis.descriptor(definition) != "staticmethod"
                            )
                            values.append(Value(functions=frozenset({CallableValue(definition, bound)})))
                field = self.fields.get((owner, node.attr))
                if field is not None:
                    values.append(field)
                if len(values) == start:
                    record = self.records.get(owner)
                    if node.attr in self.written_fields or (record is not None and node.attr in record[1]):
                        pending = True
                    elif (not definitions and any(
                        node.attr in base.__dict__.get("field_types", {})
                        for base in analysis.mro(owner)
                    )) or (definitions and all(
                        isinstance(definition, ast.Name)
                        and isinstance(declaration := analysis.parents.get(definition), ast.AnnAssign)
                        and declaration.value is None
                        for definition in definitions
                    )):
                        # An annotation alone creates no runtime attribute.
                        # Concrete subclasses supply their visible table values;
                        # the declaration is not another opaque table instance.
                        values.append(_BOTTOM)
                    else:
                        values.append(_UNKNOWN)
            if values:
                result = self.join(values)
                return result.merge(Value(unknown=receiver.unknown))
            if pending and not receiver.unknown:
                # A visible store may acquire its incoming value in a later
                # round. Bottom is pending evidence, not an opaque alternative.
                return _BOTTOM
        if isinstance(node, ast.Call):
            bound_function = self.descriptor_binding(node, seen)
            if bound_function is not None:
                return bound_function
            if node in self.container_items:
                return Value(containers=frozenset({node}))
            if self.mapping_view(node, seen):
                self.container_items.setdefault(node, _BOTTOM)
                self.mapping_views.add(node)
                return Value(containers=frozenset({node}))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "pop" and not node.args and not node.keywords:
                receiver = self.value(node.func.value, seen)
                if receiver.containers and not receiver.containers & self.container_values.keys():
                    return self.join([self.container_items[item] for item in receiver.containers]).merge(Value(unknown=receiver.unknown))
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "setdefault", "pop"}:
                receiver = self.value(node.func.value, seen)
                if receiver.containers and receiver.containers <= self.container_values.keys():
                    result = self.join([self.container_values[item] for item in receiver.containers])
                    if len(node.args) == 2:
                        result = result.merge(self.value(node.args[1], seen))
                    return result.merge(Value(unknown=receiver.unknown or bool(receiver.owners or receiver.functions)))
            builtin = analysis.bindings.get(node.func)
            if isinstance(cast(object, builtin), BuiltinBinding) and len(node.args) == 1 and not node.keywords:
                if getattr(builtin, "name", "") in {"list", "tuple", "set", "frozenset", "iter"}:
                    source = self.value(node.args[0], seen)
                    if source.containers:
                        return source
                if getattr(builtin, "name", "") == "type":
                    return self.value(node.args[0], seen)
            if (
                isinstance(cast(object, builtin), BuiltinBinding)
                and getattr(builtin, "name", "") == "getattr"
                and len(node.args) in {2, 3}
            ):
                receiver = self.value(node.args[0], seen)
                names = self.value(node.args[1], seen)
                fields = [self.member_value(receiver, name, self.class_receiver(node.args[0]), seen) for name in names.strings]
                if len(node.args) == 3:
                    fields.append(self.value(node.args[2], seen))
                if fields:
                    return self.join(fields).merge(Value(unknown=names.unknown))
            callee = self.value(node.func, seen)
            values = [self.result(item.function, seen) for item in callee.functions]
            target = self.static_target(node.func)
            if isinstance(target, ast.ClassDef):
                values.append(Value(frozenset({target})))
            if values:
                return self.join(values).merge(Value(unknown=callee.unknown))
        if isinstance(node, (ast.Await, ast.NamedExpr)):
            return self.value(node.value, seen)
        if isinstance(node, ast.Subscript):
            receiver = self.value(node.value, seen)
            if receiver.containers:
                if isinstance(node.slice, ast.Slice):
                    return receiver
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int) and not receiver.containers & self.container_values.keys():
                    return self.sequence_value(receiver, node.slice.value, seen)
                return self.join([self.container_values[item] if item in self.container_values else self.container_items[item] for item in receiver.containers]).merge(
                    Value(unknown=receiver.unknown or bool(receiver.owners or receiver.functions))
                )
        if isinstance(node, ast.IfExp):
            return self.value(node.body, seen).merge(self.value(node.orelse, seen))
        if isinstance(node, ast.BoolOp):
            return self.join([self.value(value, seen) for value in node.values])
        if isinstance(node, ast.Constant):
            # Literal values cannot hide an attribute consumer. In particular,
            # None in an optional receiver field is not an opaque alternative.
            return Value(strings=frozenset({node.value})) if isinstance(node.value, str) else _BOTTOM
        # Preserve the existing rich type/with/unpacking inference as a seed.
        if isinstance(node, ast.expr):
            inferred = self.static_receiver(node)
            if inferred:
                return Value(inferred, unknown=analysis.value_hint(node).incomplete)
        return _UNKNOWN

    def result(self, function: Function, seen: frozenset[ast.AST]) -> Value:
        if function in seen:
            return _BOTTOM
        return self.join([self.value(node, seen | {function}) for node in self.returns[function]])

    def targets(self, call: ast.Call) -> Value:
        value = self.value(call.func)
        target = self.static_target(call.func)
        if isinstance(target, ast.ClassDef):
            binding = self.analysis.member(target, "__init__")
            definitions = self.analysis.definitions(binding) if binding is not None else []
            return Value(functions=frozenset(
                CallableValue(definition, True) for definition in definitions
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
            ))
        return value

    def local_forwarder(self, decorator: ast.FunctionDef) -> bool:
        """Recognize local wraps/signature decorators that only call with forwarded inputs."""
        if decorator not in self.local_forwarders:
            self.local_forwarders[decorator] = False  # Stop recursive or cyclic factories.
            self.local_forwarders[decorator] = self.check_local_forwarder(decorator)
        return self.local_forwarders[decorator]

    def check_local_forwarder(self, decorator: ast.FunctionDef) -> bool:
        analysis = self.analysis
        args = decorator.args
        if (decorator.decorator_list or len(args.posonlyargs + args.args) != 1
                or args.vararg or args.kwarg or args.kwonlyargs):
            return False
        parameter = analysis.bindings[(*args.posonlyargs, *args.args)[0]]
        ret = decorator.body[-1]
        if not isinstance(ret, ast.Return) or not isinstance(ret.value, ast.Name):
            return False
        wrapper = self.static_target(ret.value)
        if (not isinstance(wrapper, (ast.FunctionDef, ast.AsyncFunctionDef))
                or analysis.parents.get(wrapper) is not decorator):
            return False
        positional, keywords = wrapper.args.vararg, wrapper.args.kwarg
        if not positional or not keywords or wrapper.args.posonlyargs or wrapper.args.args or wrapper.args.kwonlyargs:
            return False
        wrapper_binding = analysis.bindings[wrapper]
        if set(wrapper_binding.references) != {wrapper, ret.value}:
            return False
        if len(wrapper.decorator_list) != 1:
            return False
        wraps = wrapper.decorator_list[0]
        if (not isinstance(wraps, ast.Call) or analysis.imported_path(wraps.func) != "functools.wraps"
                or len(wraps.args) != 1 or wraps.keywords or analysis.bindings.get(wraps.args[0]) is not parameter):
            return False
        positional_binding, keywords_binding = analysis.bindings[positional], analysis.bindings[keywords]
        for binding, argument in ((positional_binding, positional), (keywords_binding, keywords)):
            for reference in binding.references:
                if reference is argument:
                    continue
                parent = analysis.parents.get(reference)
                if binding is positional_binding and isinstance(parent, ast.Starred) and parent.value is reference:
                    continue
                if binding is keywords_binding and isinstance(parent, ast.keyword) and parent.arg is None:
                    continue
                return False

        def forwarded(call: ast.Call) -> bool:
            return (
                len(call.args) == len(call.keywords) == 1
                and isinstance(call.args[0], ast.Starred)
                and analysis.bindings.get(call.args[0].value) is positional_binding
                and call.keywords[0].arg is None
                and analysis.bindings.get(call.keywords[0].value) is keywords_binding
            )

        invokes = False
        for use in parameter.references:
            if isinstance(use, ast.arg):
                continue
            parent = analysis.parents.get(use)
            if isinstance(parent, ast.Attribute) and parent.value is use and parent.attr == "__name__" and isinstance(parent.ctx, ast.Load):
                continue
            if not isinstance(parent, ast.Call):
                return False
            if parent.func is use and forwarded(parent):
                invokes = True
            elif parent is wraps:
                continue
            elif len(parent.args) == 1 and parent.args[0] is use and not parent.keywords:
                if analysis.imported_path(parent.func) == "inspect.signature":
                    continue
                factory = self.static_target(parent.func)
                assignment = analysis.parents.get(parent)
                if (not isinstance(factory, ast.FunctionDef) or not self.local_forwarder(factory)
                        or not isinstance(assignment, ast.Assign) or assignment.value is not parent
                        or len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name)):
                    return False
                alias = analysis.bindings[assignment.targets[0]]
                for reference in alias.references:
                    if reference is assignment.targets[0]:
                        continue
                    call = analysis.parents.get(reference)
                    if not isinstance(call, ast.Call) or call.func is not reference or not forwarded(call):
                        return False
                    invokes = True
            else:
                return False
        return invokes

    def call_arguments(self, call: ast.Call, target: CallableValue) -> dict[ast.arg, ast.expr] | None:
        key = (call, target)
        if any(isinstance(argument, ast.Starred) for argument in call.args) or any(keyword.arg is None for keyword in call.keywords):
            return self.bind_arguments(call, target)
        if key not in self.argument_bindings:
            self.argument_bindings[key] = self.bind_arguments(call, target)
        return self.argument_bindings[key]

    def binding_preserved(self, function: Function) -> bool:
        """Prove decorators retain the original receiver and argument binding."""
        return (
            isinstance(function, ast.Lambda) or not function.decorator_list
            or self.analysis.descriptor(function) is not None
            or bool(self.analysis.forwarding_decorators(function))
            or all(
                (isinstance(expression, ast.Call) and self.wraps_call(expression))
                or (isinstance(decorator := self.static_target(expression), ast.FunctionDef)
                    and self.local_forwarder(decorator)) for expression in function.decorator_list
            )
        )

    def wraps_call(self, call: ast.Call) -> bool:
        """functools.wraps copies metadata; it does not dispatch its argument."""
        if self.analysis.imported_path(call.func) != "functools.wraps" or len(call.args) != 1 or call.keywords:
            return False
        wrapped = self.value(call.args[0])
        return not wrapped.owners and not wrapped.containers

    def bind_arguments(self, call: ast.Call, target: CallableValue) -> dict[ast.arg, ast.expr] | None:
        """Bind visible positional/keyword arguments exactly, or reject expansion."""
        function = target.function
        if not self.binding_preserved(function):
            return None
        positional = [*function.args.posonlyargs, *function.args.args]
        if target.bound:
            positional = positional[1:]
        arguments: list[ast.expr] = []
        relay: ast.expr | None = None
        for supplied in call.args:
            if not isinstance(supplied, ast.Starred):
                arguments.append(supplied)
                continue
            incoming = self.value(supplied.value)
            if (function.args.vararg is not None and len(arguments) == len(positional)
                    and supplied is call.args[-1]):
                relay = supplied.value
                break
            lengths = {
                len(self.sequence_sources[item]) if isinstance(item, ast.Tuple) and item in self.sequence_sources else len(item.elts)
                for item in incoming.containers if isinstance(item, (ast.Tuple, ast.List))
            }
            if incoming.unknown or incoming.owners or incoming.functions or not incoming.containers or len(lengths) != 1 or any(
                not isinstance(item, (ast.Tuple, ast.List)) for item in incoming.containers
            ):
                return None
            for index in range(next(iter(lengths))):
                projection_key = supplied.value, index
                if projection_key not in self.projections:
                    self.projections[projection_key] = ast.Subscript(value=supplied.value, slice=ast.Constant(index), ctx=ast.Load())
                arguments.append(self.projections[projection_key])
        if len(arguments) > len(positional) and function.args.vararg is None:
            return None
        result = dict(zip(positional, arguments, strict=False))
        if function.args.vararg is not None and relay is not None:
            result[function.args.vararg] = relay
        elif function.args.vararg is not None:
            pack_key = call, target
            if pack_key not in self.positional_packs:
                packed_args = ast.Tuple(elts=arguments[len(positional):], ctx=ast.Load())
                self.positional_packs[pack_key] = packed_args
                self.container_items[packed_args] = _BOTTOM
            packed_args = self.positional_packs[pack_key]
            packed_args.elts = arguments[len(positional):]
            self.positional_sources[packed_args] = tuple(arguments[len(positional):])
            result[function.args.vararg] = packed_args
        keywords: dict[str, ast.expr] = {}
        extra: list[tuple[str | None, ast.expr]] = []
        for keyword in call.keywords:
            if keyword.arg is not None:
                if keyword.arg in keywords:
                    return None
                keywords[keyword.arg] = keyword.value
            elif isinstance(keyword.value, ast.Dict):
                for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or key.value in keywords:
                        return None
                    keywords[key.value] = value
            else:
                mapping = self.value(keyword.value)
                if (mapping.containers and not mapping.unknown and not mapping.owners and not mapping.functions
                        and all(self.container_items[item] == _BOTTOM and self.container_values.get(item) == _BOTTOM for item in mapping.containers)):
                    continue
                extra.append((None, keyword.value))
        named = {argument.arg: argument for argument in [*function.args.args, *function.args.kwonlyargs]}
        for name, value in keywords.items():
            argument = named.get(name)
            if argument is None:
                extra.append((name, value))
                continue
            if argument in result:
                return None
            result[argument] = value
        if extra:
            if function.args.kwarg is None:
                return None
            if any(name is None for name, _ in extra) and any(
                argument not in result for argument in [*positional, *function.args.kwonlyargs]
            ):
                return None
        if function.args.kwarg is not None:
            pack_key = call, target
            if pack_key not in self.keyword_packs:
                self.keyword_packs[pack_key] = ast.Dict(keys=[], values=[])
            packed = self.keyword_packs[pack_key]
            self.keyword_sources[packed] = tuple(extra)
            self.container_items.setdefault(packed, _BOTTOM)
            self.container_values.setdefault(packed, _BOTTOM)
            result[function.args.kwarg] = packed
        defaults = dict(zip(
            [*function.args.posonlyargs, *function.args.args][-len(function.args.defaults):],
            function.args.defaults, strict=False,
        )) if function.args.defaults else {}
        defaults.update({
            argument: default for argument, default in zip(
                function.args.kwonlyargs, function.args.kw_defaults, strict=True,
            ) if default is not None
        })
        for argument in [*positional, *function.args.kwonlyargs]:
            if argument not in result:
                if argument not in defaults:
                    return None
                result[argument] = defaults[argument]
        return result

    def add_argument(self, argument: ast.arg, value: Value) -> None:
        binding = self.analysis.bindings.get(argument)
        if binding is not None:
            previous = self.arguments.get(binding, _BOTTOM)
            updated = previous.merge(value)
            if updated is not previous:
                self.arguments[binding] = updated
                self.changed = True

    def store(self, destination: ast.expr, expression: ast.expr) -> None:
        if isinstance(destination, ast.Attribute):
            owners = self.value(destination.value).owners
            value = self.value(expression)
            if not owners:
                self.open_callbacks(value)
            for owner in owners:
                self.add_field(owner, destination.attr, value)
        elif isinstance(destination, ast.Subscript) and self.value(destination.value).containers:
            for container in self.value(destination.value).containers:
                if container in self.container_values:
                    self.add_container_value(container, self.value(destination.slice), self.value(expression))
        elif (
            isinstance(destination, (ast.Tuple, ast.List))
            and isinstance(expression, (ast.Tuple, ast.List))
            and len(destination.elts) == len(expression.elts)
            and not any(isinstance(item, ast.Starred) for item in [*destination.elts, *expression.elts])
        ):
            for item, value_node in zip(destination.elts, expression.elts, strict=True):
                self.store(item, value_node)
        elif not isinstance(destination, ast.Name):
            self.open_callbacks(self.value(expression))

    def solve(self) -> None:
        # Plain name stores never participate in the transfer function. Keep
        # the remaining nodes in their original order: each round observes
        # the same sequence of argument/field updates as a full tree scan.
        nodes = [
            node for node in self.nodes
            if isinstance(node, (ast.Call, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.GeneratorExp))
            or (isinstance(node, ast.Assign) and any(not isinstance(target, ast.Name) for target in node.targets))
            or (isinstance(node, ast.AnnAssign) and node.value is not None and not isinstance(node.target, ast.Name))
        ]
        while True:
            self.changed = False
            self.values.clear()
            for node in nodes:
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and self.mapping_view(node):
                        self.mapping_views.add(node)
                        view_receiver = self.value(node.func.value)
                        items = self.join([
                            self.container_values[item] if node.func.attr == "values" else self.container_items[item]
                            for item in view_receiver.containers
                        ])
                        previous = self.container_items.get(node, _BOTTOM)
                        updated = previous.merge(items)
                        if node not in self.container_items or updated is not previous:
                            self.container_items[node] = updated
                            self.changed = True
                    if node in self.zipped:
                        pair = self.zipped[node]
                        previous = self.container_items[pair]
                        updated = previous.merge(self.join([self.iterated_value(self.value(source)) for source in self.sequence_sources[pair] if source is not None]))
                        if updated is not previous:
                            self.container_items[pair] = updated
                            self.changed = True
                    elif node in self.container_items and len(node.args) == 1:
                        source = self.value(node.args[0])
                        incoming = self.join([self.container_items[item] for item in source.containers])
                        if source.unknown or source.owners or source.functions or not source.containers:
                            incoming = incoming.merge(_UNKNOWN)
                        previous = self.container_items[node]
                        updated = previous.merge(incoming)
                        if updated is not previous:
                            self.container_items[node] = updated
                            self.changed = True
                    if isinstance(node.func, ast.Attribute) and node.func.attr in {"add", "append"} and len(node.args) == 1 and not node.keywords:
                        for container in self.value(node.func.value).containers:
                            previous = self.container_items[container]
                            updated = previous.merge(self.value(node.args[0]))
                            if updated is not previous:
                                self.container_items[container] = updated
                                self.changed = True
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault" and len(node.args) in {1, 2} and not node.keywords:
                        for container in self.value(node.func.value).containers:
                            if container in self.container_values:
                                self.add_container_value(container, self.value(node.args[0]), self.value(node.args[1]) if len(node.args) == 2 else _BOTTOM)
                    builtin = self.analysis.bindings.get(node.func)
                    if (
                        isinstance(cast(object, builtin), BuiltinBinding)
                        and getattr(builtin, "name", "") == "setattr" and len(node.args) == 3
                    ):
                        receiver, name, expression = node.args
                        for owner in self.value(receiver).owners:
                            names = {name.value} if isinstance(name, ast.Constant) and isinstance(name.value, str) else self.written_fields
                            for field_name in names:
                                self.add_field(owner, field_name, self.value(expression))
                    constructor = self.static_target(node.func)
                    if isinstance(constructor, ast.ClassDef) and constructor in self.records:
                        self.record_arguments(constructor, node)
                    targets = self.targets(node)
                    for target in targets.functions:
                        if target.bound and target.receivers:
                            positional = [*target.function.args.posonlyargs, *target.function.args.args]
                            if positional:
                                self.add_argument(positional[0], Value(target.receivers))
                        arguments = self.call_arguments(node, target)
                        if arguments is not None:
                            for argument, expression in arguments.items():
                                if isinstance(expression, ast.Tuple) and expression in self.positional_sources:
                                    previous = self.container_items[expression]
                                    updated = previous.merge(self.join([self.value(source) for source in self.positional_sources[expression]]))
                                    if updated is not previous:
                                        self.container_items[expression] = updated
                                        self.changed = True
                                if isinstance(expression, ast.Dict) and expression in self.keyword_sources:
                                    for keyword_name, keyword_source in self.keyword_sources[expression]:
                                        incoming = self.value(keyword_source)
                                        if keyword_name is not None:
                                            self.add_container_value(expression, Value(strings=frozenset({keyword_name})), incoming)
                                        else:
                                            keys = self.join([self.container_items[item] for item in incoming.containers])
                                            values = self.join([self.container_values.get(item, _UNKNOWN) for item in incoming.containers])
                                            if incoming.unknown or incoming.owners or incoming.functions or not incoming.containers:
                                                keys, values = keys.merge(_UNKNOWN), values.merge(_UNKNOWN)
                                            self.add_container_value(expression, keys, values)
                                self.add_argument(argument, self.value(expression))
                        else:
                            if any(
                                isinstance(supplied, ast.Starred) and self.value(supplied.value) == _BOTTOM
                                for supplied in node.args
                            ):
                                continue
                            # Failed explicit argument binding cannot change
                            # the receiver already captured by a bound method.
                            positional = [*target.function.args.posonlyargs, *target.function.args.args]
                            if target.bound and self.binding_preserved(target.function):
                                positional = positional[1:]
                            for unknown_argument in [*positional,
                                             *target.function.args.kwonlyargs, target.function.args.vararg, target.function.args.kwarg]:
                                if unknown_argument is not None:
                                    self.add_argument(unknown_argument, _UNKNOWN)
                    if (targets.unknown or not targets.functions) and not (
                        node in self.container_items or self.passive_container_call(node)
                        or self.descriptor_binding(node) is not None
                        or self.wraps_call(node)
                    ):
                        for expression in [*node.args, *(keyword.value for keyword in node.keywords)]:
                            self.open_callbacks(self.value(expression))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    destinations = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for destination in destinations:
                        self.store(destination, node.value)
                elif isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                    if node in self.container_items:
                        if isinstance(node, ast.Dict):
                            for key, value in zip(node.keys, node.values, strict=True):
                                self.add_container_value(node, self.value(key) if key is not None else _UNKNOWN, self.value(value))
                        else:
                            previous = self.container_items[node]
                            elements = [node.elt] if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) else node.elts
                            updated = previous.merge(self.join([
                                self.iterated_value(self.value(element.value))
                                if isinstance(element, ast.Starred) else self.value(element)
                                for element in elements
                            ]))
                            if updated is not previous:
                                self.container_items[node] = updated
                                self.changed = True
                        continue
                    parent = self.analysis.parents.get(node)
                    if isinstance(parent, ast.keyword) and parent.arg is None:
                        call = self.analysis.parents.get(parent)
                        if isinstance(call, ast.Call) and any(
                            self.call_arguments(call, target) is not None for target in self.targets(call).functions
                        ):
                            continue
                    elements = node.values if isinstance(node, ast.Dict) else (
                        [node.elt] if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) else node.elts
                    )
                    for element in elements:
                        self.open_callbacks(self.value(element))
            if not self.changed:
                break
        self.values.clear()

    def add_container_value(self, container: ast.expr, key: Value, value: Value) -> None:
        previous_keys, previous_values = self.container_items[container], self.container_values[container]
        keys, values = previous_keys.merge(key), previous_values.merge(value)
        if keys is not previous_keys or values is not previous_values:
            self.container_items[container], self.container_values[container] = keys, values
            self.changed = True

    def add_field(self, owner: ast.ClassDef, name: str, value: Value) -> None:
        key = (owner, name)
        previous = self.fields.get(key, _BOTTOM)
        updated = previous.merge(value)
        if updated is not previous:
            self.fields[key] = updated
            self.changed = True

    def open_callbacks(self, value: Value, seen: frozenset[Function] = frozenset(), containers: frozenset[ast.expr] = frozenset()) -> None:
        # An external consumer may invoke these with arbitrary arguments. Merely
        # seeing one safe internal caller must not close an escaping callback API.
        for container in value.containers - containers:
            items = self.container_items[container].merge(self.container_values.get(container, _BOTTOM))
            self.open_callbacks(items, seen, containers | {container})
        for target in value.functions:
            if target.function in seen:
                continue
            positional = [*target.function.args.posonlyargs, *target.function.args.args]
            if target.bound:
                positional = positional[1:]
            if positional or target.function.args.kwonlyargs or target.function.args.vararg or target.function.args.kwarg:
                self.opaque_callbacks.add(target.function)
            for argument in [*positional, *target.function.args.kwonlyargs]:
                self.add_argument(argument, _UNKNOWN)
            self.open_callbacks(self.result(target.function, frozenset()), seen | {target.function}, containers)

    def receivers(self, node: ast.expr) -> set[ast.ClassDef]:
        return set(self.value(node).owners)

    def describe(self, node: ast.AST) -> str:
        function = self.enclosing_function(node)
        origin = function if function is not None else node
        source = node.__dict__.get("original_file", origin.__dict__.get("original_file", "<staging>"))
        scope = ":".join(self.analysis.path(function)) if function is not None else "<module>"
        line = node.__dict__.get("original_lineno", getattr(node, "lineno", "?"))
        return f"{ast.unparse(node)!r} at {source}:{line} ({scope})"

    def imported_namespace(self, node: ast.expr) -> bool:
        """An imported module is not an unresolved application instance."""
        if not isinstance(node, ast.Name):
            return False
        binding = self.analysis.bindings.get(node)
        definitions = self.analysis.definitions(binding) if binding is not None else []
        return len(definitions) == 1 and isinstance(definitions[0], ast.alias) and (
            self.analysis.imports.get(definitions[0], ("", ""))[1] is None
        )

    def unrelated(self, node: ast.expr, family: set[ast.ClassDef]) -> bool:
        if self.value(node).owners & family:
            return False
        if self.imported_namespace(node):
            return True
        hint = self.external_returns.hint(node)
        if not hint.classes or hint.self_reference or hint.incomplete:
            return False
        for path in hint.classes:
            if path in self.analysis.annotation_classes:
                if self.analysis.annotation_classes[path] in family:
                    return False
                continue
            if path not in self.external_types:
                parts = path.split(".")
                resolved: type | None = None
                for index in range(len(parts) - 1, 0, -1):
                    try:
                        resolved = self.analysis.external_members.resolve(".".join(parts[:index]), parts[index:])
                        break
                    except (ImportError, AttributeError, ValueError):
                        continue
                self.external_types[path] = resolved
            external = self.external_types[path]
            if external is None or external in {object, type}:
                return False
            for owner in family:
                for base in self.analysis.mro(owner):
                    for parent in base.bases:
                        if isinstance(self.analysis.target(parent, instances=False), ast.ClassDef):
                            continue
                        if external in self.analysis.external_base(parent).__mro__:
                            return False
        return True

    def container_operation(self, call: ast.Call, receiver: Value) -> bool:
        if not isinstance(call.func, ast.Attribute) or not receiver.containers or call.keywords:
            return False
        if call.func.attr == "clear":
            return not call.args
        if receiver.containers <= self.container_values.keys():
            return (call.func.attr in {"get", "setdefault", "pop"} and len(call.args) in {1, 2}) or (
                call.func.attr in {"keys", "values"} and not call.args and not receiver.unknown
            )
        if not receiver.containers & self.container_values.keys():
            return (call.func.attr in {"add", "append", "discard", "remove"} and len(call.args) == 1) or (
                call.func.attr == "pop" and not call.args
            )
        return False

    def mapping_view(self, call: ast.Call, seen: frozenset[ast.AST] = frozenset()) -> bool:
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {"keys", "values"} or call.args or call.keywords:
            return False
        receiver = self.value(call.func.value, seen)
        return bool(receiver.containers) and receiver.containers <= self.container_values.keys() and not (
            receiver.unknown or receiver.owners or receiver.functions
        )

    def passive_container_call(self, call: ast.Call) -> bool:
        if not isinstance(call.func, ast.Attribute):
            return False
        receiver = self.value(call.func.value)
        return not receiver.unknown and not receiver.owners and not receiver.functions and self.container_operation(call, receiver)

    def related_fields(self, owner: ast.ClassDef, family: set[ast.ClassDef]) -> set[str] | None:
        if owner in family:
            return None
        fields: set[str] = set()
        for base in self.analysis.mro(owner):
            if any(binding.name in {"__getattr__", "__getattribute__"} for binding in self.analysis.node_bindings(base)):
                return None
            for (declaring, name), value in self.fields.items():
                if declaring is base and self.exposes(value, family):
                    fields.add(name)
            for binding in self.analysis.node_bindings(base):
                for definition in self.analysis.definitions(binding):
                    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) and self.exposes(self.result(definition, frozenset()), family):
                        fields.add(binding.name)
                    if isinstance(definition, ast.Name):
                        parent = self.analysis.parents.get(definition)
                        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None and self.exposes(self.value(parent.value), family):
                            fields.add(binding.name)
        return fields

    def context_argument(self, expression: ast.expr) -> Value:
        if isinstance(expression, ast.Tuple) and expression in self.positional_sources:
            self.container_items[expression] = self.join([self.value(source) for source in self.positional_sources[expression]])
        if isinstance(expression, ast.Dict) and expression in self.keyword_sources:
            keys, values = _BOTTOM, _BOTTOM
            for name, source in self.keyword_sources[expression]:
                incoming = self.value(source)
                if name is not None:
                    keys = keys.merge(Value(strings=frozenset({name})))
                    values = values.merge(incoming)
                else:
                    keys = keys.merge(self.join([self.container_items[item] for item in incoming.containers]))
                    values = values.merge(self.join([self.container_values.get(item, _UNKNOWN) for item in incoming.containers]))
                    if incoming.unknown or incoming.owners or incoming.functions or not incoming.containers:
                        keys, values = keys.merge(_UNKNOWN), values.merge(_UNKNOWN)
            self.container_items[expression], self.container_values[expression] = keys, values
        return self.value(expression)

    def visit_context(self, function: Function, parameters: dict[ast.arg, ast.expr], family: set[ast.ClassDef], covered: set[ast.Call]) -> None:
        if function in self.context_active or function in self.opaque_callbacks:
            return
        old_arguments, old_values = self.arguments, self.values
        old_items, old_mapping = self.container_items, self.container_values
        self.container_items, self.container_values = dict(old_items), dict(old_mapping)
        incoming = {
            self.analysis.bindings[parameter]: self.context_argument(expression)
            for parameter, expression in parameters.items() if parameter in self.analysis.bindings
        }
        self.arguments = {**old_arguments, **incoming}
        self.values = {}
        self.context_active.add(function)
        try:
            for call in self.function_calls.get(function, ()):
                self.verify_call(call, family)
                targets = self.targets(call)
                for target in targets.functions:
                    arguments = self.call_arguments(call, target)
                    if arguments is not None:
                        self.visit_context(target.function, arguments, family, covered)
                if targets.unknown or not targets.functions:
                    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
                        for callback in self.value(argument).functions:
                            positional = [*callback.function.args.posonlyargs, *callback.function.args.args]
                            if callback.bound:
                                positional = positional[1:]
                            if not positional and not callback.function.args.kwonlyargs and not callback.function.args.vararg and not callback.function.args.kwarg:
                                self.visit_context(callback.function, {}, family, covered)
                covered.add(call)
        finally:
            self.context_active.remove(function)
            self.arguments, self.values = old_arguments, old_values
            self.container_items, self.container_values = old_items, old_mapping

    def prepare_contexts(self, family: set[ast.ClassDef]) -> None:
        identity = frozenset(family)
        if identity in self.context_covered:
            return
        covered: set[ast.Call] = set()
        for node in self.nodes:
            if not isinstance(node, ast.Call) or any(isinstance(argument, ast.Starred) for argument in node.args):
                continue
            supplied = [*node.args, *(keyword.value for keyword in node.keywords)]
            if not any(self.exposes(self.value(argument), family) for argument in supplied):
                continue
            callbacks = [callback for argument in supplied for callback in self.value(argument).functions]
            if any(not isinstance(self.analysis.parents.get(callback.function), (ast.ClassDef, ast.Module)) for callback in callbacks):
                continue
            for target in self.targets(node).functions:
                if target.function.args.vararg is None and not callbacks:
                    continue
                parameters = self.call_arguments(node, target)
                if parameters is not None:
                    self.visit_context(target.function, parameters, family, covered)
        self.context_covered[identity] = covered

    def verify_contextual_call(self, call: ast.Call, family: set[ast.ClassDef]) -> None:
        try:
            self.verify_call(call, family)
        except ValueError:
            function = self.enclosing_function(call)
            if call not in self.context_covered.get(frozenset(family), set()) or function in self.opaque_callbacks:
                raise

    def unrelated_external_consumer(self, call: ast.Call, family: set[ast.ClassDef]) -> bool:
        path = self.analysis.imported_path(call.func)
        if path is None or any(isinstance(argument, ast.Starred) for argument in call.args) or any(keyword.arg is None for keyword in call.keywords):
            return False
        try:
            consumer = self.external_returns.resolve(path)
            if not isinstance(consumer, (type, FunctionType)):
                return False
            signature = inspect.signature(consumer, follow_wrapped=False, eval_str=False)
            bound = signature.bind(*call.args, **{keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None})
        except (ImportError, AttributeError, OSError, TypeError, ValueError):
            return False
        checked = False
        for parameter, argument in bound.arguments.items():
            if not isinstance(argument, ast.expr):
                return False
            value = self.value(argument)
            if not self.exposes(value, family):
                continue
            if value.unknown or value.containers or value.functions or not value.owners:
                return False
            accessed = self.external_usage.fields(consumer, parameter)
            if accessed is None:
                return False
            for owner in value.owners:
                related = self.related_fields(owner, family)
                if related is None or related & accessed:
                    return False
            checked = True
        return checked

    def quart_callback_registration(self, call: ast.Call, argument: ast.expr) -> bool:
        """Quart registers lifecycle and route callbacks without inspecting their receivers."""
        if not isinstance(call.func, ast.Attribute):
            return False
        if call.func.attr in {
            "before_serving", "after_serving",
            "before_request", "after_request", "teardown_request",
            "before_websocket", "after_websocket", "teardown_websocket",
        }:
            if len(call.args) != 1 or call.args[0] is not argument or call.keywords:
                return False
        elif call.func.attr == "add_url_rule":
            if any(isinstance(item, ast.Starred) for item in call.args) or any(item.arg is None for item in call.keywords):
                return False
            positional = len(call.args) == 3 and call.args[2] is argument and not any(item.arg == "view_func" for item in call.keywords)
            keyword = len(call.args) <= 2 and sum(item.arg == "view_func" and item.value is argument for item in call.keywords) == 1
            if not positional and not keyword:
                return False
        else:
            return False
        hint = self.external_returns.hint(call.func.value)
        paths = hint.classes
        if not paths and isinstance(call.func.value, ast.Name):
            binding = self.analysis.bindings.get(call.func.value)
            definitions = self.analysis.definitions(binding) if binding is not None else []
            if len(definitions) == 1 and isinstance(definitions[0], ast.arg) and definitions[0].annotation is not None:
                path = self.analysis.imported_path(definitions[0].annotation)
                paths = (path,) if path is not None else ()
        if not paths or hint.self_reference or hint.incomplete:
            return False
        try:
            from quart import Quart

            return all(self.external_returns.resolve(path) is Quart for path in paths)
        except (ImportError, AttributeError, ValueError, TypeError):
            return False

    def verify_call(self, call: ast.Call, family: set[ast.ClassDef]) -> None:
        if self.descriptor_binding(call) is not None or self.wraps_call(call):
            return
        if isinstance(call.func, ast.Attribute):
            receiver = self.value(call.func.value)
            supported_container = self.container_operation(call, receiver)
            if self.passive_container_call(call):
                return
            if receiver.containers and not supported_container and self.exposes(receiver, family):
                raise ValueError(f"Escaping allowlisted attribute receiver through unsupported container operation: {ast.unparse(call)!r}")
        builtin = self.analysis.bindings.get(call.func)
        if (isinstance(cast(object, builtin), BuiltinBinding) and getattr(builtin, "name", "") in {"getattr", "hasattr"}
                and len(call.args) in {2, 3} and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str)):
            return
        if call in self.zipped:
            return  # zip/enumerate only iterate; element flow remains tracked.
        if isinstance(cast(object, builtin), BuiltinBinding) and not call.keywords and (
            (getattr(builtin, "name", "") in {"dict", "list", "tuple", "set", "frozenset", "len", "iter", "reversed", "bool", "type"}
             and len(call.args) == 1)
            or (getattr(builtin, "name", "") in {"isinstance", "issubclass"} and len(call.args) == 2)
        ):
            # These builtins dispatch fixed protocols, not arbitrary field names.
            # Internal protocol bodies are part of this same flow analysis;
            # inherited external implementations are verified by check_receiver.
            return
        arguments = list(call.args)
        for keyword in call.keywords:
            if keyword.arg is None and isinstance(keyword.value, ast.Dict):
                arguments.extend(keyword.value.values)
            else:
                arguments.append(keyword.value)
        targets = self.targets(call)
        if targets.unknown or not targets.functions:
            for argument in arguments:
                for callback in self.value(argument).functions:
                    owner = self.analysis.parents.get(callback.function)
                    # Callback results may cross opaque dispatch boundaries.
                    # Selecting attribute renames accepts external handling of
                    # those results; only direct bound receivers escape here.
                    if (callback.bound and isinstance(owner, ast.ClassDef)
                        and (owner in family or bool(callback.receivers & family))
                        and not self.quart_callback_registration(call, argument)
                    ):
                        raise ValueError(
                            f"Escaping allowlisted attribute receiver through bound callback: {ast.unparse(call)!r}"
                        )
        affected = any(self.exposes(self.value(argument), family) for argument in arguments)
        if not affected:
            if isinstance(call.func, ast.Attribute) and self.receivers(call.func.value) & family:
                if targets.functions and not targets.unknown:
                    if any(self.call_arguments(call, target) is None for target in targets.functions):
                        raise ValueError(f"Unverifiable consumer on allowlisted attribute receiver: {ast.unparse(call)!r}")
                    return
                owners = self.receivers(call.func.value)
                for owner in owners & family:
                    # A callable stored on an instance is not a method: reading
                    # it does not bind/pass the instance as an implicit argument.
                    # Stores and explicit arguments are checked separately, as
                    # are visible bound callbacks above. Unknown methods must
                    # still fail closed; field evidence is required here.
                    if (owner, call.func.attr) in self.fields and self.analysis.member(owner, call.func.attr) is None:
                        continue
                    if not any(
                        call.func.attr in self.analysis.external_members.members(self.analysis.external_base(parent))
                        for base in self.analysis.mro(owner) for parent in base.bases
                        if not isinstance(self.analysis.target(parent, instances=False), ast.ClassDef)
                    ):
                        raise ValueError(f"Unresolved consumer on allowlisted attribute receiver: {ast.unparse(call)!r}")
            return
        constructor = isinstance(self.analysis.target(call.func, instances=False), ast.ClassDef)
        if constructor:
            constructor_owner = self.analysis.target(call.func, instances=False)
            if isinstance(constructor_owner, ast.ClassDef):
                self.analysis.check_receiver(constructor_owner)
                if constructor_owner.bases and not targets.functions:
                    raise ValueError(f"Escaping allowlisted attribute receiver into inherited constructor: {self.describe(call)}")
        if targets.unknown or (not targets.functions and not constructor):
            if self.unrelated_external_consumer(call, family):
                return
            raise ValueError(f"Escaping allowlisted attribute receiver: {self.describe(call)}; unresolved consumer")
        for target in targets.functions:
            if self.call_arguments(call, target) is None:
                raise ValueError(f"Escaping allowlisted attribute receiver: {self.describe(call)}; unverifiable arguments")

    def contained_owners(self, value: Value) -> set[ast.ClassDef]:
        owners = set(value.owners)
        pending = list(value.containers)
        seen: set[ast.expr] = set()
        while pending:
            container = pending.pop()
            if container in seen:
                continue
            seen.add(container)
            items = self.container_items[container]
            items = items.merge(self.container_values.get(container, _BOTTOM))
            for callback in items.functions:
                owner = self.analysis.parents.get(callback.function)
                if callback.bound and isinstance(owner, ast.ClassDef):
                    owners.add(owner)
                items = items.merge(self.result(callback.function, frozenset()))
            owners.update(items.owners)
            pending.extend(items.containers - seen)
        return owners

    def exposes(self, value: Value, family: set[ast.ClassDef]) -> bool:
        owners = self.contained_owners(value)
        if not owners:
            return False
        key = frozenset(family)
        if key not in self.exposed_owners:
            reachable = set(family)
            pending = list(family)
            while pending:
                current = pending.pop()
                for container in self.field_containers.get(current, ()):
                    if container not in reachable:
                        reachable.add(container)
                        pending.append(container)
            self.exposed_owners[key] = reachable
        return not owners.isdisjoint(self.exposed_owners[key])

    def verify_storage(self, node: ast.AST, family: set[ast.ClassDef]) -> None:
        if not isinstance(node, ast.expr) or isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
            return
        value = self.value(node)
        if not value.owners & family and not (value.containers and self.exposes(value, family)):
            return
        parent = self.analysis.parents.get(node)
        if isinstance(parent, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Yield, ast.YieldFrom)):
            if parent in self.container_items:
                return
            keyword = self.analysis.parents.get(parent)
            if isinstance(parent, ast.Dict) and isinstance(keyword, ast.keyword) and keyword.arg is None:
                call = self.analysis.parents.get(keyword)
                if isinstance(call, ast.Call) and self.targets(call).functions and all(
                    self.call_arguments(call, target) is not None for target in self.targets(call).functions
                ):
                    return
            raise ValueError(f"Escaping allowlisted attribute receiver in unsupported container/yield: {ast.unparse(parent)!r}")
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            for target in targets:
                if isinstance(target, (ast.Tuple, ast.List)) and all(
                    isinstance(part, ast.Name) or (isinstance(part, ast.Attribute) and self.receivers(part.value))
                    for part in target.elts
                ):
                    continue
                if isinstance(target, ast.Name):
                    continue
                if isinstance(target, ast.Attribute) and self.receivers(target.value):
                    continue
                if isinstance(target, ast.Subscript):
                    container = self.value(target.value)
                    if (container.containers and container.containers <= self.container_values.keys()
                            and not container.unknown and not container.owners and not container.functions):
                        for item in container.containers:
                            external_owner = self.external_container_owners.get(item)
                            if external_owner is not None and (not value.owners or value.unknown or value.functions or value.containers or not all(
                                any(external_owner in self.analysis.external_base(base).__mro__
                                    for owner in self.analysis.mro(receiver) for base in owner.bases
                                    if not isinstance(self.static_target(base), ast.ClassDef))
                                for receiver in value.owners
                            )):
                                raise ValueError(f"Escaping allowlisted attribute receiver outside a verified inherited namespace: {ast.unparse(parent)!r}")
                        continue
                raise ValueError(f"Escaping allowlisted attribute receiver in unresolved storage: {ast.unparse(parent)!r}")

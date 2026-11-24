from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from .external import ExternalReturns

if TYPE_CHECKING:
    from ..analysis import Binding, Function
    from .allowlist import AllowlistRenamer

__all__: list[str] = []


@dataclass(frozen=True)
class CallableValue:
    function: Function
    bound: bool = False


@dataclass(frozen=True)
class Value:
    owners: frozenset[ast.ClassDef] = frozenset()
    functions: frozenset[CallableValue] = frozenset()
    unknown: bool = False

    def merge(self, other: Value) -> Value:
        return Value(self.owners | other.owners, self.functions | other.functions, self.unknown or other.unknown)


class AttributeFlow:
    """Monotone, whole-program receiver/callback flow for opt-in field renames.

    An annotation can supply receiver information, but never proves a callback's
    implementation. Every incoming callable value must resolve to visible code.
    Joins retain unknown alternatives; recursion converges without executing code.
    """

    def __init__(self, analysis: AllowlistRenamer) -> None:
        self.analysis: AllowlistRenamer = analysis
        self.nodes: list[ast.AST] = [node for node in ast.walk(analysis.tree) if id(node) not in analysis.annotation_nodes]
        self.arguments: dict[Binding, Value] = {}
        self.fields: dict[tuple[ast.ClassDef, str], Value] = {}
        self.external_types: dict[str, type | None] = {}
        self.external_returns: ExternalReturns = ExternalReturns(analysis)
        self.values: dict[ast.AST, Value] = {}
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
        self.changed: bool = False
        self.solve()

    def enclosing_function(self, node: ast.AST) -> Function | None:
        parent = self.analysis.parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return parent
            parent = self.analysis.parents.get(parent)
        return None

    def join(self, values: list[Value]) -> Value:
        result = Value()
        for value in values:
            result = result.merge(value)
        return result

    def value(self, node: ast.AST, seen: frozenset[ast.AST] = frozenset()) -> Value:
        if node in seen:
            return Value()
        cached = self.values.get(node)
        if cached is not None:
            return cached
        value = self.expression(node, seen)
        if isinstance(node, ast.expr) and not value.owners and not value.functions:
            owners, _ = self.analysis.receiver(node)
            if owners:
                value = Value(frozenset(owners))
        self.values[node] = value
        return value

    def expression(self, node: ast.AST, seen: frozenset[ast.AST]) -> Value:
        if node in seen:
            return Value()
        seen |= {node}
        analysis = self.analysis
        if isinstance(node, ast.Lambda):
            return Value(functions=frozenset({CallableValue(node)}))
        if isinstance(node, ast.Name):
            target = analysis.target(node, instances=False)
            if isinstance(target, ast.ClassDef):
                return Value(frozenset({target}))
            binding = analysis.bindings.get(node)
            definitions = analysis.definitions(binding) if binding is not None else []
            values: list[Value] = []
            for definition in definitions:
                if isinstance(definition, ast.arg) and binding is not None:
                    inferred, _ = analysis.receiver(node)
                    values.append(self.arguments.get(binding, Value()).merge(Value(frozenset(inferred))))
                elif isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    values.append(Value(functions=frozenset({CallableValue(definition)})))
                elif isinstance(definition, ast.ClassDef):
                    values.append(Value(frozenset({definition})))
                elif isinstance(definition, ast.Name):
                    parent = analysis.parents.get(definition)
                    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                        values.append(self.value(parent.value, seen))
                    else:
                        inferred, _ = analysis.receiver(node)
                        values.append(Value(frozenset(inferred), unknown=not inferred))
                else:
                    values.append(Value(unknown=True))
            if values:
                return self.join(values)
        if isinstance(node, ast.Attribute):
            receiver = self.value(node.value, seen)
            owners = receiver.owners
            values = []
            for owner in owners:
                start = len(values)
                binding = analysis.member(owner, node.attr)
                definitions = analysis.definitions(binding) if binding is not None else []
                for definition in definitions:
                    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if analysis.is_property(definition):
                            values.append(self.result(definition, seen))
                        else:
                            class_access = analysis.target(node.value, instances=False) is owner
                            bound = analysis.descriptor(definition) == "classmethod" or (
                                not class_access and analysis.descriptor(definition) != "staticmethod"
                            )
                            values.append(Value(functions=frozenset({CallableValue(definition, bound)})))
                field = self.fields.get((owner, node.attr))
                if field is not None:
                    values.append(field)
                if len(values) == start and node.attr not in self.written_fields:
                    values.append(Value(unknown=True))
            if values:
                result = self.join(values)
                return result.merge(Value(unknown=receiver.unknown))
            if owners and not receiver.unknown and node.attr in self.written_fields:
                # A visible store may acquire its incoming value in a later
                # round. Bottom is pending evidence, not an opaque alternative.
                return Value()
        if isinstance(node, ast.Call):
            builtin = analysis.bindings.get(node.func)
            if (
                isinstance(cast(object, builtin), BuiltinBinding)
                and getattr(builtin, "name", "") == "getattr"
                and len(node.args) in {2, 3}
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
            ):
                receiver = self.value(node.args[0], seen)
                fields = [self.fields[owner, node.args[1].value] for owner in receiver.owners
                          if (owner, node.args[1].value) in self.fields]
                if len(node.args) == 3:
                    fields.append(self.value(node.args[2], seen))
                if fields:
                    return self.join(fields)
            callee = self.value(node.func, seen)
            values = [self.result(item.function, seen) for item in callee.functions]
            target = analysis.target(node.func, instances=False)
            if isinstance(target, ast.ClassDef):
                values.append(Value(frozenset({target})))
            if values:
                return self.join(values).merge(Value(unknown=callee.unknown))
        if isinstance(node, (ast.Await, ast.NamedExpr)):
            return self.value(node.value, seen)
        if isinstance(node, ast.IfExp):
            return self.value(node.body, seen).merge(self.value(node.orelse, seen))
        if isinstance(node, ast.BoolOp):
            return self.join([self.value(value, seen) for value in node.values])
        if isinstance(node, ast.Constant):
            # Literal values cannot hide an attribute consumer. In particular,
            # None in an optional receiver field is not an opaque alternative.
            return Value()
        # Preserve the existing rich type/with/unpacking inference as a seed.
        if isinstance(node, ast.expr):
            inferred, _ = analysis.receiver(node)
            if inferred:
                return Value(frozenset(inferred))
        return Value(unknown=True)

    def result(self, function: Function, seen: frozenset[ast.AST]) -> Value:
        if function in seen:
            return Value()
        return self.join([self.value(node, seen | {function}) for node in self.returns[function]])

    def targets(self, call: ast.Call) -> Value:
        value = self.value(call.func)
        target = self.analysis.target(call.func, instances=False)
        if isinstance(target, ast.ClassDef):
            binding = self.analysis.member(target, "__init__")
            definitions = self.analysis.definitions(binding) if binding is not None else []
            return Value(functions=frozenset(
                CallableValue(definition, True) for definition in definitions
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
            ))
        return value

    def call_arguments(self, call: ast.Call, target: CallableValue) -> dict[ast.arg, ast.expr] | None:
        """Bind visible positional/keyword arguments exactly, or reject expansion."""
        function = target.function
        if not isinstance(function, ast.Lambda) and function.decorator_list and (
            self.analysis.descriptor(function) is None
            and not self.analysis.forwarding_decorators(function)
        ):
            return None
        positional = [*function.args.posonlyargs, *function.args.args]
        if target.bound:
            positional = positional[1:]
        arguments = list(call.args)
        if any(isinstance(argument, ast.Starred) for argument in arguments):
            return None
        if len(arguments) > len(positional):
            return None
        result = dict(zip(positional, arguments, strict=False))
        keywords: dict[str, ast.expr] = {}
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
                return None
        named = {argument.arg: argument for argument in [*function.args.args, *function.args.kwonlyargs]}
        for name, value in keywords.items():
            argument = named.get(name)
            if argument is None or argument in result:
                return None
            result[argument] = value
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
            previous = self.arguments.get(binding, Value())
            updated = previous.merge(value)
            if updated != previous:
                self.arguments[binding] = updated
                self.changed = True

    def solve(self) -> None:
        while True:
            self.changed = False
            self.values.clear()
            for node in self.nodes:
                if isinstance(node, ast.Call):
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
                    targets = self.targets(node)
                    for target in targets.functions:
                        arguments = self.call_arguments(node, target)
                        if arguments is not None:
                            for argument, expression in arguments.items():
                                self.add_argument(argument, self.value(expression))
                        else:
                            for argument in [*target.function.args.posonlyargs, *target.function.args.args,
                                             *target.function.args.kwonlyargs]:
                                self.add_argument(argument, Value(unknown=True))
                    if targets.unknown or not targets.functions:
                        for expression in [*node.args, *(keyword.value for keyword in node.keywords)]:
                            self.open_callbacks(self.value(expression))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    destinations = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for destination in destinations:
                        if isinstance(destination, ast.Attribute):
                            owners = self.value(destination.value).owners
                            if not owners:
                                self.open_callbacks(self.value(node.value))
                            for owner in owners:
                                self.add_field(owner, destination.attr, self.value(node.value))
                        elif not isinstance(destination, ast.Name):
                            self.open_callbacks(self.value(node.value))
                elif isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
                    parent = self.analysis.parents.get(node)
                    if isinstance(parent, ast.keyword) and parent.arg is None:
                        call = self.analysis.parents.get(parent)
                        if isinstance(call, ast.Call) and any(
                            self.call_arguments(call, target) is not None for target in self.targets(call).functions
                        ):
                            continue
                    elements = node.values if isinstance(node, ast.Dict) else node.elts
                    for element in elements:
                        self.open_callbacks(self.value(element))
            if not self.changed:
                break
        self.values.clear()

    def add_field(self, owner: ast.ClassDef, name: str, value: Value) -> None:
        key = (owner, name)
        previous = self.fields.get(key, Value())
        updated = previous.merge(value)
        if updated != previous:
            self.fields[key] = updated
            self.changed = True

    def open_callbacks(self, value: Value, seen: frozenset[Function] = frozenset()) -> None:
        # An external consumer may invoke these with arbitrary arguments. Merely
        # seeing one safe internal caller must not close an escaping callback API.
        for target in value.functions:
            if target.function in seen:
                continue
            positional = [*target.function.args.posonlyargs, *target.function.args.args]
            if target.bound:
                positional = positional[1:]
            for argument in [*positional, *target.function.args.kwonlyargs]:
                self.add_argument(argument, Value(unknown=True))
            self.open_callbacks(self.result(target.function, frozenset()), seen | {target.function})

    def receivers(self, node: ast.expr) -> set[ast.ClassDef]:
        return set(self.value(node).owners)

    def describe(self, node: ast.AST) -> str:
        function = self.enclosing_function(node)
        origin = function if function is not None else node
        source = origin.__dict__.get("original_file", "<staging>")
        scope = ":".join(self.analysis.path(function)) if function is not None else "<module>"
        return f"{ast.unparse(node)!r} at {source}:{getattr(node, 'lineno', '?')} ({scope})"

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
        if not hint.classes or hint.origin is not None:
            return False
        for path in hint.classes:
            if path in self.analysis.annotation_classes:
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
            if external is None:
                return False
            for owner in family:
                for base in self.analysis.mro(owner):
                    for parent in base.bases:
                        if isinstance(self.analysis.target(parent, instances=False), ast.ClassDef):
                            continue
                        if external in self.analysis.external_base(parent).__mro__:
                            return False
        return True

    def verify_call(self, call: ast.Call, family: set[ast.ClassDef]) -> None:
        builtin = self.analysis.bindings.get(call.func)
        if isinstance(cast(object, builtin), BuiltinBinding) and not call.keywords and (
            (getattr(builtin, "name", "") in {"dict", "list", "tuple", "set", "frozenset", "len", "iter", "bool", "type"}
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
                    if self.exposes(self.result(callback.function, frozenset()), family) or (
                        callback.bound and isinstance(owner, ast.ClassDef)
                        and self.exposes(Value(frozenset({owner})), family)
                    ):
                        raise ValueError(
                            f"Escaping allowlisted attribute receiver returned by callback: {ast.unparse(call)!r}"
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
            raise ValueError(f"Escaping allowlisted attribute receiver: {self.describe(call)}; unresolved consumer")
        for target in targets.functions:
            if self.call_arguments(call, target) is None:
                raise ValueError(f"Escaping allowlisted attribute receiver: {self.describe(call)}; unverifiable arguments")

    def exposes(self, value: Value, family: set[ast.ClassDef], seen: frozenset[ast.ClassDef] = frozenset()) -> bool:
        if value.owners & family:
            return True
        owners = value.owners - seen
        return any(
            self.exposes(field, family, seen | owners)
            for (owner, _), field in self.fields.items() if owner in owners
        )

    def verify_storage(self, node: ast.AST, family: set[ast.ClassDef]) -> None:
        if not isinstance(node, ast.expr) or not self.value(node).owners & family:
            return
        parent = self.analysis.parents.get(node)
        if isinstance(parent, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Yield, ast.YieldFrom)):
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
                if isinstance(target, ast.Name):
                    continue
                if isinstance(target, ast.Attribute) and self.receivers(target.value):
                    continue
                raise ValueError(f"Escaping allowlisted attribute receiver in unresolved storage: {ast.unparse(parent)!r}")

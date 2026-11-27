from __future__ import annotations

import ast
from typing import cast

from python_minifier.rename.binding import BuiltinBinding

from .annotations import TypeHint
from .debug import record_attribute
from .renamers.base import ArgumentRenamer

__all__: list[str] = ["minify_dataclasses"]


class _Dataclasses(ArgumentRenamer):
    """Plan all edits before mutating the tree; uncertainty preserves the API."""

    def __init__(self, tree: ast.Module) -> None:
        super().__init__({"__main__": tree})
        self.tree = tree
        self.classes = {node for node in self.nodes if isinstance(node, ast.ClassDef)}
        self.records = {
            node
            for node in self.classes
            if any(self.dataclass_decorator(decorator) for decorator in node.decorator_list)
        }
        self.preserved: set[ast.ClassDef] = set()
        self.named: set[ast.ClassDef] = set()
        self.dependencies: dict[ast.ClassDef, set[ast.ClassDef]] = {}

    def hint_classes(self, hint: TypeHint, owner: ast.ClassDef | None = None) -> set[ast.ClassDef]:
        result = self.hint_types(hint, owner)
        for argument in hint.arguments:
            result.update(self.hint_classes(argument, owner))
        return result

    def values(self, node: ast.AST) -> set[ast.ClassDef]:
        return self.hint_classes(self.value_hint(node), self.enclosing_class(node))

    def builtin_call(self, call: ast.Call, names: set[str]) -> bool:
        return (
            isinstance(call.func, ast.Name)
            and call.func.id in names
            and isinstance(cast(object, self.bindings.get(call.func)), BuiltinBinding)
        )

    def call_hint(self, node: ast.Call, seen: set[int]) -> TypeHint:
        if self.builtin_call(node, {"list", "tuple", "reversed"}) and len(node.args) == 1:
            return TypeHint(
                origin="sequence", arguments=(self.value_hint(node.args[0], seen).iterated(),)
            )
        return super().call_hint(node, seen)

    def container_call(self, call: ast.Call, targets: set[ast.ClassDef]) -> bool:
        """Recognize operations that store records without inspecting their fields."""
        if self.builtin_call(call, {"list", "tuple", "reversed", "len", "bool"}):
            return len(call.args) == 1 and not call.keywords
        if isinstance(call.func, ast.Attribute) and call.func.attr in {"append", "extend"}:
            receiver = self.value_hint(call.func.value)
            return (
                receiver.origin == "sequence"
                and not receiver.classes
                and targets <= self.values(call.func.value)
                and len(call.args) == 1
                and not call.keywords
            )
        return False

    def tracked_binding(self, node: ast.Name, targets: set[ast.ClassDef]) -> bool:
        binding = self.bindings.get(node)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) == 1:
            return True
        return bool(definitions) and all(
            isinstance(definition, ast.Name)
            and isinstance(definition.ctx, ast.Store)
            and targets
            <= self.hint_classes(
                self.destructured_hint(definition, set()), self.enclosing_class(node)
            )
            for definition in definitions
        )

    def argument_is_tracked(
        self, call: ast.Call, value: ast.AST, targets: set[ast.ClassDef]
    ) -> bool:
        """Require the actual destination parameter to describe every record value."""
        callee = self.target(call.func, instances=False)
        keyword = next((item.arg for item in call.keywords if item.value is value), None)
        position = next((index for index, item in enumerate(call.args) if item is value), None)
        if keyword is None and position is None:
            return False
        if any(isinstance(item, ast.Starred) for item in call.args):
            return False
        bound = False
        if isinstance(callee, ast.ClassDef):
            if callee in self.records:
                fields = cast(dict[str, TypeHint], callee.__dict__.get("field_types", {}))
                names = list(fields)
                name = (
                    keyword
                    if keyword is not None
                    else (
                        names[position] if position is not None and position < len(names) else None
                    )
                )
                hint = fields.get(name) if name is not None else None
                return hint is not None and targets <= self.hint_classes(hint, callee)
            binding = self.member_binding(callee, "__init__")
            definitions = self.definitions(binding) if binding is not None else []
            callee = definitions[0] if len(definitions) == 1 else None
            bound = True
        elif (
            isinstance(callee, (ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(call.func, ast.Attribute)
            and isinstance(self.parents.get(callee), ast.ClassDef)
        ):
            bound = self.descriptor(callee) != "staticmethod"
        if not isinstance(callee, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        positional = [*callee.args.posonlyargs, *callee.args.args][int(bound) :]
        parameter = (
            next(
                (item for item in (*positional, *callee.args.kwonlyargs) if item.arg == keyword),
                None,
            )
            if keyword is not None
            else positional[position]
            if position is not None and position < len(positional)
            else None
        )
        hint = parameter.__dict__.get("type_annotation") if parameter is not None else None
        return isinstance(hint, TypeHint) and targets <= self.hint_classes(
            hint, self.enclosing_class(callee)
        )

    def escaping_functions(self) -> set[ast.FunctionDef | ast.AsyncFunctionDef]:
        """Track escaped signatures without treating internal inheritance as reflection."""
        result: set[ast.FunctionDef | ast.AsyncFunctionDef] = set()
        for function in self.functions:
            if isinstance(function, ast.Lambda):
                continue
            if (
                function.decorator_list
                and self.descriptor(function) is None
                and not self.forwarding_decorators(function)
                and not self.is_property(function)
            ):
                result.add(function)
            owner = self.parents.get(function)
            if isinstance(owner, ast.ClassDef) and any(
                base.keywords
                or not self.safe_class_decorators(base)
                or any(
                    self.target(parent, instances=False) not in self.classes
                    for parent in base.bases
                )
                for base in self.class_mro(owner, set())
            ):
                result.add(function)
        for node in self.nodes:
            if id(node) in self.annotation_nodes or not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            target = self.target(node)
            if not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parent = self.parents.get(node)
            if (
                not (isinstance(parent, ast.Call) and parent.func is node)
                and self.alias_assignment(node) is None
                and not self.is_property(target)
            ):
                result.add(target)
        return result

    def closure(self, roots: set[ast.ClassDef]) -> set[ast.ClassDef]:
        result = set(roots)
        pending = list(roots)
        while pending:
            for child in self.dependencies.get(pending.pop(), set()) - result:
                result.add(child)
                pending.append(child)
        return result

    def plain_default(self, value: ast.expr) -> bool:
        if isinstance(value, ast.Call) and self.dataclasses_field_call(value):
            return not value.args and all(
                item.arg is not None and (item.arg != "default" or self.plain_default(item.value))
                for item in value.keywords
            )
        try:
            ast.literal_eval(value)
        except ValueError, TypeError, SyntaxError:
            return False
        return True

    def scan(self) -> None:
        constructed = {
            self.target(node.func, instances=False)
            for node in self.nodes
            if isinstance(node, ast.Call)
        }
        # A definition with no visible Python constructor is not evidence of a
        # closed class: it may be an externally instantiated schema/export.
        self.preserved.update(self.records - constructed)
        assignments_by_owner: dict[int, list[ast.Assign | ast.AnnAssign]] = {}
        for (identity, _), assignments in self.instance_assignments.items():
            assignments_by_owner.setdefault(identity, []).extend(assignments)
        for owner in self.classes:
            dependencies: set[ast.ClassDef] = set()
            fields = cast(dict[str, TypeHint], owner.__dict__.get("field_types", {}))
            for hint in fields.values():
                dependencies.update(self.hint_classes(hint, owner))
            for assignment in assignments_by_owner.get(id(owner), ()):
                if assignment.value is not None:
                    dependencies.update(self.values(assignment.value))
            self.dependencies[owner] = dependencies
            for base in owner.bases:
                self.preserved.update(self.values(base) & self.records)
            if owner not in self.records:
                if owner.keywords or not self.safe_class_decorators(owner):
                    self.preserved.add(owner)
                continue
            if owner.bases or owner.keywords or not self.safe_class_decorators(owner):
                self.preserved.add(owner)
            for statement in owner.body:
                if isinstance(statement, ast.AnnAssign):
                    if statement.value is not None and not self.plain_default(statement.value):
                        # Unknown descriptors can observe the original field name.
                        self.preserved.add(owner)
                    # These are dataclass directives, not ordinary field types.
                    if any(
                        (isinstance(part, ast.Constant) and isinstance(part.value, str))
                        or self.imported_path(part)
                        in {"typing.ClassVar", "dataclasses.InitVar", "dataclasses.KW_ONLY"}
                        for part in ast.walk(statement.annotation)
                    ):
                        self.preserved.add(owner)
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if (
                        (statement.decorator_list and self.descriptor(statement) is None)
                        or statement.name
                        in {"__new__", "__getattr__", "__getattribute__", "__setattr__"}
                        or (owner in self.records and statement.name == "__init__")
                    ):
                        self.preserved.add(owner)
                elif isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id.startswith("__")
                    for target in statement.targets
                ):
                    self.preserved.add(owner)

        for function in self.escaping_functions():
            for argument in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            ):
                parameter_hint = argument.__dict__.get("type_annotation")
                if isinstance(parameter_hint, TypeHint) and argument.arg not in {"self", "cls"}:
                    self.preserved.update(
                        self.hint_classes(parameter_hint, self.enclosing_class(function))
                    )
            return_hint = function.__dict__.get("return_type")
            if isinstance(return_hint, TypeHint):
                self.preserved.update(
                    self.hint_classes(return_hint, self.enclosing_class(function))
                )

        for node in self.nodes:
            if id(node) in self.annotation_nodes:
                continue
            if isinstance(node, ast.Call):
                path = self.imported_path(node.func)
                if path == "dataclasses.asdict":
                    operand = (
                        node.args[0]
                        if node.args
                        else next((item.value for item in node.keywords if item.arg == "obj"), None)
                    )
                    targets: set[ast.ClassDef] = (
                        self.values(operand) if operand is not None else set()
                    )
                    # Unknown operands are protected where their record values
                    # leave tracked flows; they do not expose unrelated records.
                    self.named.update(targets)
                if path == "dacite.from_dict":
                    # Resolve known roots, including container annotations. For
                    # dynamic class arguments, protect class objects where they
                    # escape into registries/wrappers below; an unrelated external
                    # schema must not disable every internal record optimization.
                    roots = set[ast.ClassDef]().union(
                        *(self.values(item) for item in node.args),
                        *(self.values(item.value) for item in node.keywords),
                    )
                    self.preserved.update(roots)
            if not isinstance(
                node,
                (
                    ast.Name,
                    ast.Attribute,
                    ast.Call,
                    ast.Subscript,
                    ast.List,
                    ast.Tuple,
                    ast.Set,
                    ast.Dict,
                    ast.NamedExpr,
                    ast.IfExp,
                    ast.BoolOp,
                ),
            ):
                continue
            if isinstance(node, (ast.Name, ast.Attribute)) and not isinstance(node.ctx, ast.Load):
                continue
            if isinstance(node, ast.Name):
                binding = self.bindings.get(node)
                definitions = self.definitions(binding) if binding is not None else []
                if len(definitions) != 1:
                    # Rebound class names have no unique lexical target. Each
                    # possible definition remains an externally visible schema.
                    self.preserved.update(
                        definition
                        for definition in definitions
                        if isinstance(definition, ast.ClassDef)
                    )
            targets = self.values(node)
            if not targets:
                continue
            parent = self.parents.get(node)
            if isinstance(node, ast.Name) and self.target(node, instances=False) in self.classes:
                if isinstance(parent, ast.Call) and parent.func is node:
                    continue
                if self.alias_assignment(node) is not None:
                    continue
                if isinstance(parent, ast.ClassDef) and node in parent.bases:
                    continue  # Dataclass bases were protected explicitly above.
                # Passing a class to dacite, a registry, or reflection is unsafe.
                self.preserved.update(targets)
                continue
            if isinstance(parent, ast.Attribute) and parent.value is node:
                if parent.attr.startswith("__"):
                    self.preserved.update(targets)
                continue
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                destinations = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                if not all(
                    (
                        targets <= self.values(destination)
                        and (
                            not isinstance(destination, ast.Name)
                            or self.tracked_binding(destination, targets)
                        )
                    )
                    or self.instance_attribute_owner(destination) is not None
                    for destination in destinations
                ):
                    self.preserved.update(targets)
                continue
            if isinstance(parent, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Subscript)):
                continue
            if isinstance(parent, ast.keyword):
                parent = self.parents.get(parent)
            if isinstance(parent, ast.Call):
                if parent.func is node:
                    continue
                path = self.imported_path(parent.func)
                if path == "dataclasses.asdict":
                    continue
                if self.container_call(parent, targets):
                    continue
                if self.argument_is_tracked(parent, node, targets):
                    continue
                if self.dataclasses_field_call(parent):
                    continue
                # An opaque consumer can inspect inherited instance state too.
                for target in targets:
                    self.preserved.update(self.class_mro(target, set()))
            if isinstance(parent, ast.Return):
                if all(self.returns_instance(parent, node, target) for target in targets):
                    continue  # Escaping function signatures were checked above.
            if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node:
                continue
            if isinstance(parent, ast.Expr):
                continue  # A discarded value exposes neither names nor annotations.
            if isinstance(
                parent,
                (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.If, ast.IfExp, ast.Assert, ast.While),
            ):
                continue
            self.preserved.update(targets)

        self.preserved = self.closure(self.preserved)
        self.named = self.closure(self.named | self.preserved)

    def run(self) -> None:
        if not self.records or self.frame_inspection or getattr(self.tree, "tainted", False):
            return
        # Namespace reflection can expose even classes not mentioned as operands.
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"globals", "locals", "vars", "eval", "exec", "dir"}
            for node in self.nodes
        ):
            return
        self.scan()
        fields: dict[ast.ClassDef, dict[str, ast.AnnAssign]] = {
            owner: {
                statement.target.id: statement
                for statement in owner.body
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
            }
            for owner in self.nodes
            if isinstance(owner, ast.ClassDef) and owner in self.records
        }
        calls: dict[ast.ClassDef, list[ast.Call]] = {owner: [] for owner in self.records}
        attributes: dict[tuple[ast.ClassDef, str], list[ast.Attribute]] = {}
        field_owners: dict[str, list[ast.ClassDef]] = {}
        for owner, members in fields.items():
            for name in members:
                field_owners.setdefault(name, []).append(owner)
        for node in self.nodes:
            self.used.update(binding.name for binding in self.node_bindings(node))
            if isinstance(node, ast.Attribute):
                self.used.add(node.attr)
                if id(node) in self.annotation_nodes or node.attr not in field_owners:
                    continue
                owners = self.type_targets(node.value)
                for owner in field_owners[node.attr]:
                    if owner not in owners:
                        # A record entering an unresolved flow is preserved by
                        # scan(). An unrelated opaque receiver cannot expose a
                        # record whose entire flow remained tracked.
                        continue
                    if owners != {owner}:
                        # Unknown and union receivers must never be rewritten by spelling.
                        self.named.add(owner)
                    else:
                        attributes.setdefault((owner, node.attr), []).append(node)
            elif isinstance(node, ast.Call):
                target = self.target(node.func, instances=False)
                if isinstance(target, ast.ClassDef) and target in calls:
                    calls[target].append(node)
                    if any(
                        item.arg is None and self.literal_keywords(item) is None
                        for item in node.keywords
                    ):
                        self.named.add(target)
            elif isinstance(node, ast.MatchClass):
                self.named.update(self.values(node.cls))
        self.used.update(self.strings)
        for owner, members in fields.items():
            if owner in self.preserved:
                continue
            for statement in members.values():
                statement.annotation = ast.copy_location(ast.Constant(0), statement.annotation)
            if owner in self.named or any(name.startswith("__") for name in members):
                continue
            for name, statement in members.items():
                new = self.fresh_name()
                record_attribute(owner, name, new)
                binding = self.bindings.get(statement.target)
                if binding is not None:
                    binding.rename(new)
                else:
                    cast(ast.Name, statement.target).id = new
                for attribute in attributes.get((owner, name), []):
                    attribute.attr = new
                for call in calls[owner]:
                    for item in call.keywords:
                        if item.arg == name:
                            item.arg = new
                        elif item.arg is None:
                            for key in self.literal_keywords(item) or []:
                                if key.value == name:
                                    key.value = new
                # Keep the captured types usable by subsequent renaming passes.
                hints = cast(dict[str, TypeHint], owner.__dict__.get("field_types", {}))
                if name in hints:
                    hints[new] = hints.pop(name)
        ast.fix_missing_locations(self.tree)


def minify_dataclasses(tree: ast.Module) -> None:
    """Strip closed dataclass annotations and rename proven-private fields."""
    _Dataclasses(tree).run()

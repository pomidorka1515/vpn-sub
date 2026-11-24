from __future__ import annotations

import ast
from typing import cast

from .analysis import inspects_frames
from .annotations import TypeHint
from .renamers.base import ArgumentRenamer

__all__: list[str] = ["minify_dataclasses"]


class _Dataclasses(ArgumentRenamer):
    """Plan all edits before mutating the tree; uncertainty preserves the API."""

    def __init__(self, tree: ast.Module) -> None:
        super().__init__({"__main__": tree})
        self.tree = tree
        self.nodes = list(ast.walk(tree))
        self.classes = {node for node in self.nodes if isinstance(node, ast.ClassDef)}
        self.records = {
            node
            for node in self.classes
            if any(self.dataclass_decorator(decorator) for decorator in node.decorator_list)
        }
        self.preserved: set[ast.ClassDef] = set()
        self.named: set[ast.ClassDef] = set()
        self.dependencies: dict[ast.ClassDef, set[ast.ClassDef]] = {}

    def imported_path(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> str | None:
        if id(node) in seen:
            return None
        seen |= {id(node)}
        if isinstance(node, ast.Attribute):
            prefix = self.imported_path(node.value, seen)
            return f"{prefix}.{node.attr}" if prefix else None
        binding = self.bindings.get(node)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) != 1:
            return None
        definition = definitions[0]
        if isinstance(definition, ast.alias):
            module, name = self.imports.get(definition, ("", None))
            return f"{module}.{name}" if name else module
        parent = self.parents.get(definition)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
            return self.imported_path(parent.value, seen)
        return None

    def hint_classes(self, hint: TypeHint, owner: ast.ClassDef | None = None) -> set[ast.ClassDef]:
        result = self.hint_types(hint, owner)
        for argument in hint.arguments:
            result.update(self.hint_classes(argument, owner))
        return result

    def values(self, node: ast.AST) -> set[ast.ClassDef]:
        return self.hint_classes(self.value_hint(node), self.enclosing_class(node))

    def callee_accepts_argument(self, call: ast.Call, target: ast.ClassDef) -> bool:
        if super().callee_accepts_argument(call, target):
            return True
        callee = self.target(call.func, instances=False)
        if isinstance(callee, ast.ClassDef):
            fields = cast(dict[str, TypeHint], callee.__dict__.get("field_types", {}))
            return any(target in self.hint_classes(hint, callee) for hint in fields.values())
        if isinstance(callee, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (*callee.args.posonlyargs, *callee.args.args, *callee.args.kwonlyargs):
                hint = argument.__dict__.get("type_annotation")
                if isinstance(hint, TypeHint) and target in self.hint_classes(
                    hint, self.enclosing_class(callee)
                ):
                    return True
        return False

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
        for owner in self.classes:
            dependencies: set[ast.ClassDef] = set()
            fields = cast(dict[str, TypeHint], owner.__dict__.get("field_types", {}))
            for hint in fields.values():
                dependencies.update(self.hint_classes(hint, owner))
            for (identity, _), assignments in self.instance_assignments.items():
                if identity == id(owner):
                    for assignment in assignments:
                        if assignment.value is not None:
                            dependencies.update(self.values(assignment.value))
            self.dependencies[owner] = dependencies
            if owner.bases or owner.keywords or not self.safe_class_decorators(owner):
                self.preserved.add(owner)
            for base in owner.bases:
                self.preserved.update(self.values(base))
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

        _, _, reflected = self.analyze_calls()
        for function in reflected:
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
                    self.named.update(targets or self.records)
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
                    targets <= self.values(destination)
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
                if all(self.callee_accepts_argument(parent, target) for target in targets):
                    continue
                if self.dataclasses_field_call(parent):
                    continue
            if isinstance(parent, ast.Return):
                if all(self.returns_instance(parent, node, target) for target in targets):
                    continue  # Escaping function signatures were checked above.
            if isinstance(
                parent, (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.If, ast.IfExp, ast.Assert)
            ):
                continue
            self.preserved.update(targets)

        self.preserved = self.closure(self.preserved)
        self.named = self.closure(self.named | self.preserved)

    def run(self) -> None:
        if not self.records or inspects_frames(self.tree) or getattr(self.tree, "tainted", False):
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
        for node in self.nodes:
            self.used.update(binding.name for binding in self.node_bindings(node))
            if isinstance(node, ast.Attribute):
                self.used.add(node.attr)
                if id(node) in self.annotation_nodes:
                    continue
                owners = self.type_targets(node.value)
                for owner, members in fields.items():
                    if node.attr not in members:
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

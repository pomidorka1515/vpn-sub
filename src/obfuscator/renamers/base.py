from __future__ import annotations

import ast
import keyword
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from python_minifier.ast_annotation import add_parent
from python_minifier.rename.bind_names import bind_names
from python_minifier.rename.binding import BuiltinBinding
from python_minifier.rename.mapper import add_namespace
from python_minifier.rename.resolve_names import resolve_names

from ..analysis import Binding, annotation_nodes, inspects_frames
from ..annotations import TypeHint, merge_hints
from ..inference import bind_call, join_hints, operation_hint, unify_hint

if TYPE_CHECKING:
    from ..analysis import Function

__all__: list[str] = ["ArgumentRenamer"]

_EMPTY_SYMBOLS: MappingProxyType[str, Binding] = MappingProxyType({})
_RESERVED_NAMES: frozenset[str] = frozenset((*keyword.kwlist, "self", "cls"))
type CallAnalysis = tuple[
    dict[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, list[ast.Call]],
    set[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda],
    set[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda],
]


class ArgumentRenamer:
    """Rename closed, statically resolved signatures across the staging tree.

    Single-assignment aliases, instances and cross-module calls are resolved lexically.
    Closed receiver parameters and stdlib context-manager decorators are supported.
    Escaping callables, unknown decorators, inheritance, unknown method receivers and
    dynamic keyword dictionaries preserve signatures. Unrelated attribute names
    and string values do not prevent renaming a closed function's parameters.
    Variadic bindings are local names; renaming them never changes kwargs keys.
    """

    def __init__(self, trees: dict[str, ast.Module]) -> None:
        self.hint_cache: dict[tuple[ast.AST, frozenset[int]], TypeHint] = {}
        self.root_hints: dict[ast.AST, TypeHint] = {}
        self.imported_paths: dict[ast.AST, str | None] = {}
        self._definitions: dict[Binding, list[ast.AST]] = {}
        self._class_mros: dict[ast.ClassDef, list[ast.ClassDef]] = {}
        self._member_bindings: dict[tuple[ast.ClassDef, str], Binding | None] = {}
        self.trees = trees
        self.parents: dict[ast.AST, ast.AST] = {}
        self.enclosing_classes: dict[ast.AST, ast.ClassDef | None] = {}
        self.bindings: dict[ast.AST, Binding] = {}
        self.modules: dict[ast.AST, str] = {}
        self.symbols: dict[str, dict[str, Binding]] = {}
        self.imports: dict[ast.alias, tuple[str, str | None]] = {}
        self.star_imports: set[str] = set()
        self.functions: list[Function] = []
        self.nodes: list[ast.AST] = []
        self.module_nodes: dict[str, list[ast.AST]] = {}
        self.used = set(_RESERVED_NAMES)
        self.strings: set[str] = set()
        self.next_name = 0
        self.renamed = 0
        self.skipped: Counter[str] = Counter()
        self.annotation_nodes: set[int] = set()
        self.annotation_classes: dict[str, ast.ClassDef] = {}
        self.annotation_aliases: dict[str, TypeHint] = {
            path: hint
            for tree in trees.values()
            for path, hint in tree.__dict__.get("annotation_aliases", {}).items()
        }
        self.canonical_hints: dict[TypeHint, TypeHint] = {}
        assignments: list[ast.Assign | ast.AnnAssign] = []
        for module, tree in trees.items():
            # Use the minifier's lexical resolver rather than text replacement:
            # closures, nonlocal, comprehensions and shadowing need real bindings.
            for operation in (add_parent, add_namespace, bind_names, resolve_names):
                cast(Callable[[ast.Module], None], operation)(tree)
            self.symbols[module] = {binding.name: binding for binding in self.node_bindings(tree)}
            # Breadth-first, like ast.walk, but discover children only once:
            # the same edges supply both the traversal and the parent index.
            nodes: list[ast.AST] = [tree]
            for node in nodes:
                self.modules[node] = module
                for child in ast.iter_child_nodes(node):
                    self.parents[child] = node
                    nodes.append(child)  # ruff: ignore[loop-iterator-mutation] - Breadth-first queue.
                bindings = node.__dict__.get("bindings")
                if bindings:
                    for binding in cast(Sequence[Binding], bindings):
                        self.bindings.update((ref, binding) for ref in binding.references)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    self.functions.append(node)
                elif isinstance(node, ast.ClassDef):
                    path = node.__dict__.get("annotation_path")
                    if isinstance(path, str):
                        self.annotation_classes[path] = node
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    assignments.append(node)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.strings.add(node.value)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.imports[alias] = (
                            alias.name if alias.asname else alias.name.split(".")[0],
                            None,
                        )
                elif isinstance(node, ast.ImportFrom):
                    package = module.split(".")[:-1]
                    if getattr(tree, "is_package", False):
                        package = module.split(".")
                    if node.level:
                        package = package[: len(package) - node.level + 1]
                        target = ".".join([*package, *([node.module] if node.module else [])])
                    else:
                        target = node.module or ""
                    for alias in node.names:
                        self.imports[alias] = (target, alias.name)
                        if alias.name == "*":
                            self.star_imports.add(target)
            self.nodes.extend(nodes)
            self.module_nodes[module] = nodes
        self.annotation_nodes = annotation_nodes(self.nodes)
        self.instance_assignments: dict[tuple[int, str], list[ast.Assign | ast.AnnAssign]] = {}
        for assignment in assignments:
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target_node in targets:
                if isinstance(target_node, ast.Attribute):
                    owner = self.instance_attribute_owner(target_node)
                    if owner is not None:
                        self.instance_assignments.setdefault(
                            (id(owner), target_node.attr), []
                        ).append(assignment)

    @cached_property
    def frame_inspection(self) -> bool:
        """Compute frame taint once, and only for passes that need it."""
        return inspects_frames(self.nodes)

    @staticmethod
    def node_bindings(node: ast.AST) -> Sequence[Binding]:
        return cast(Sequence[Binding], node.__dict__.get("bindings") or ())

    def descriptor(self, function: Function) -> str | None:
        """Recognize only unshadowed builtin method descriptors, not arbitrary decorators."""
        if isinstance(function, ast.Lambda) or len(function.decorator_list) != 1:
            return None
        decorator = function.decorator_list[0]
        if not isinstance(self.parents.get(function), ast.ClassDef):
            return None
        path = self.imported_path(decorator)
        if path in {"builtins.staticmethod", "builtins.classmethod"}:
            return path.removeprefix("builtins.")
        return None

    def imported_path(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> str | None:
        if not seen:
            if node not in self.imported_paths:
                self.imported_paths[node] = self._imported_path(node, seen)
            return self.imported_paths[node]
        return self._imported_path(node, seen)

    def _imported_path(self, node: ast.AST, seen: frozenset[int]) -> str | None:
        if id(node) in seen:
            return None
        seen |= {id(node)}
        if isinstance(node, ast.Attribute):
            prefix = self.imported_path(node.value, seen)
            return f"{prefix}.{node.attr}" if prefix else None
        binding = self.bindings.get(node)
        if isinstance(cast(object, binding), BuiltinBinding):
            return f"builtins.{getattr(binding, 'name', '')}"
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

    def dataclass_decorator(self, decorator: ast.expr) -> bool:
        """Recognize dataclasses, whose generated members never inspect methods."""
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        reference = target.value if isinstance(target, ast.Attribute) else target
        binding = self.bindings.get(reference)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            return False
        module, name = self.imports.get(definitions[0], ("", None))
        if isinstance(target, ast.Attribute):
            if name is not None:
                return False
            name = target.attr
        return module == "dataclasses" and name == "dataclass"

    def safe_class_decorators(self, owner: ast.ClassDef) -> bool:
        return all(self.dataclass_decorator(decorator) for decorator in owner.decorator_list)

    def dataclasses_field_call(self, call: ast.Call) -> bool:
        """Recognize ``dataclasses.field(...)`` factories and their defaults."""
        reference = call.func.value if isinstance(call.func, ast.Attribute) else call.func
        binding = self.bindings.get(reference)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            return False
        module, name = self.imports.get(definitions[0], ("", None))
        if isinstance(call.func, ast.Attribute):
            if name is not None:
                return False
            name = call.func.attr
        return module == "dataclasses" and name == "field"

    def callee_accepts_argument(self, call: ast.Call, target: ast.ClassDef) -> bool:
        """Whether a known callee annotates the argument with the instance class."""
        callee = self.target(call.func, instances=False)
        if isinstance(callee, ast.ClassDef):
            fields = cast(object, callee.__dict__.get("field_types"))
            if isinstance(fields, dict):
                for hint in cast(dict[str, object], fields).values():
                    if isinstance(hint, TypeHint) and target in self.hint_types(hint, callee):
                        return True
            initializer = next(
                (
                    child
                    for child in callee.body
                    if isinstance(child, ast.FunctionDef) and child.name == "__init__"
                ),
                None,
            )
            return initializer is not None and self.function_accepts_argument(
                initializer, target, callee
            )
        if isinstance(callee, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = self.parents.get(callee)
            return self.function_accepts_argument(
                callee, target, owner if isinstance(owner, ast.ClassDef) else None
            )
        return False

    def function_accepts_argument(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        target: ast.ClassDef,
        owner: ast.ClassDef | None,
    ) -> bool:
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ):
            hint = argument.__dict__.get("type_annotation")
            if isinstance(hint, TypeHint) and target in self.hint_types(hint, owner):
                return True
        return False

    def stores_instance(self, use: ast.AST, receiver: ast.AST) -> bool:
        """Whether an instance is only stored in a resolvable ``self`` attribute."""
        if not isinstance(use, (ast.Assign, ast.AnnAssign)) or use.value is not receiver:
            return False
        targets = use.targets if isinstance(use, ast.Assign) else [use.target]
        return bool(targets) and all(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            for target in targets
        )

    def returns_instance(self, use: ast.AST, receiver: ast.AST, target: ast.ClassDef) -> bool:
        """Whether an instance is returned with a known receiver or annotated type."""
        if not isinstance(use, ast.Return):
            return False
        if isinstance(receiver, ast.Name) and receiver.id in {"self", "cls"}:
            return True
        function = self.parents.get(use)
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        hint = function.__dict__.get("return_type")
        owner = self.parents.get(function)
        return isinstance(hint, TypeHint) and target in self.hint_types(
            hint, owner if isinstance(owner, ast.ClassDef) else None
        )

    def alias_assignment(self, node: ast.AST) -> ast.Name | None:
        """Return the destination of a lexically single-assignment alias."""
        assignment = self.parents.get(node)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is not node:
            return None
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return None
        destination = targets[0]
        target = self.target(node)
        return destination if target is not None and self.target(destination) is target else None

    def forwarding_decorators(self, function: Function) -> bool:
        """Trust only imported stdlib decorators that forward the original arguments."""
        if isinstance(function, ast.Lambda):
            return False
        supported = {"contextmanager", "asynccontextmanager"}
        for decorator in function.decorator_list:
            reference = decorator.value if isinstance(decorator, ast.Attribute) else decorator
            binding = self.bindings.get(reference)
            definitions = self.definitions(binding) if binding is not None else []
            if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
                return False
            module, name = self.imports.get(definitions[0], ("", None))
            if isinstance(decorator, ast.Attribute):
                if name is not None:
                    return False
                name = decorator.attr
            if module != "contextlib" or module in self.trees or name not in supported:
                return False
        return bool(function.decorator_list)

    def definitions(self, binding: Binding) -> list[ast.AST]:
        definitions = self._definitions.get(binding)
        if definitions is None:
            definitions = [
                ref
                for ref in binding.references
                if not isinstance(ref, (ast.Name, ast.Global, ast.Nonlocal))
                or (isinstance(ref, ast.Name) and not isinstance(ref.ctx, ast.Load))
            ]
            self._definitions[binding] = definitions
        return definitions

    def imported_attribute(self, module: str, attribute: ast.Attribute) -> bool:
        """An external re-export is not a reflective use of its containing module."""
        if not isinstance(attribute.ctx, ast.Load) or attribute.attr.startswith("__"):
            return False
        binding = self.symbols.get(module, _EMPTY_SYMBOLS).get(attribute.attr)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            return False
        imported_module, _ = self.imports.get(definitions[0], ("", None))
        return bool(imported_module) and imported_module not in self.trees

    def binding_target(
        self, binding: Binding | None, seen: set[int], *, instances: bool = True
    ) -> ast.AST | str | None:
        if binding is None or id(binding) in seen:
            return None
        seen = seen.union({id(binding)})
        definitions = self.definitions(binding)
        if len(definitions) != 1:
            return None
        definition = definitions[0]
        if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return definition
        if isinstance(definition, ast.Name) and isinstance(definition.ctx, ast.Store):
            assignment = self.parents.get(definition)
            if (
                isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and (not isinstance(assignment, ast.Assign) or assignment.targets == [definition])
                and isinstance(assignment.value, (ast.Name, ast.Attribute, ast.Lambda))
            ):
                return self.target(assignment.value, seen, instances=instances)
            if (
                instances
                and isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and (not isinstance(assignment, ast.Assign) or assignment.targets == [definition])
                and isinstance(assignment.value, ast.Call)
            ):
                owner = self.target(assignment.value.func, seen, instances=False)
                # Only ordinary constructors are known to return an instance
                # of their class. Factories, metaclasses and __new__ may not.
                if isinstance(owner, ast.ClassDef) and not (
                    owner.bases
                    or owner.keywords
                    or owner.decorator_list
                    or any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name == "__new__"
                        for child in owner.body
                    )
                ):
                    return owner
        if isinstance(definition, ast.alias) and definition in self.imports:
            module, name = self.imports[definition]
            if name is None:
                return module if module in self.trees else None
            target = self.binding_target(
                self.symbols.get(module, _EMPTY_SYMBOLS).get(name), seen, instances=instances
            )
            return (
                target
                if target is not None
                else (f"{module}.{name}" if f"{module}.{name}" in self.trees else None)
            )
        if instances and isinstance(definition, ast.arg):
            function = self.parents.get(self.parents.get(definition, definition))
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = self.parents.get(function)
                if (
                    isinstance(owner, ast.ClassDef)
                    and self.descriptor(function) != "staticmethod"
                    and (positional := function.args.posonlyargs + function.args.args)
                    and positional[0] is definition
                ):
                    return owner
        return None

    def instance_attribute_owner(self, target: ast.AST) -> ast.ClassDef | None:
        """Return the class whose receiver is assigned through ``self.attr``."""
        if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name):
            return None
        binding = self.bindings.get(target.value)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) != 1 or not isinstance(definitions[0], ast.arg):
            return None
        argument = definitions[0]
        function = self.parents.get(self.parents.get(argument, argument))
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        if (
            self.descriptor(function) == "staticmethod"
            or not (positional := function.args.posonlyargs + function.args.args)
            or positional[0] is not argument
        ):
            return None
        owner = self.parents.get(function)
        return owner if isinstance(owner, ast.ClassDef) else None

    def enclosing_class(self, node: ast.AST) -> ast.ClassDef | None:
        if node in self.enclosing_classes:
            return self.enclosing_classes[node]
        scope = self.parents.get(node)
        while scope is not None:
            if isinstance(scope, ast.ClassDef):
                self.enclosing_classes[node] = scope
                return scope
            scope = self.parents.get(scope)
        self.enclosing_classes[node] = None
        return None

    def hint_types(self, hint: TypeHint, owner: ast.ClassDef | None) -> set[ast.ClassDef]:
        hint = self.canonical_hint(hint)
        result: set[ast.ClassDef] = set()
        if hint.self_reference and owner is not None:
            result.add(owner)
        for path in hint.classes:
            target = self.annotation_classes.get(path)
            if target is not None:
                result.add(target)
        return result

    def class_mro(self, owner: ast.ClassDef, seen: set[int]) -> list[ast.ClassDef]:
        root = not seen
        if root and owner in self._class_mros:
            return self._class_mros[owner]
        if id(owner) in seen:
            return []
        seen = set(seen)
        seen.add(id(owner))
        result = [owner]
        for base in owner.bases:
            base = base.value if isinstance(base, ast.Subscript) else base
            target = self.binding_target(
                self.bindings.get(base) if isinstance(base, ast.Name) else None,
                set(),
                instances=False,
            )
            if not isinstance(target, ast.ClassDef) and isinstance(base, ast.Attribute):
                target = self.target(base, set(), instances=False)
            if isinstance(target, ast.ClassDef) and target not in result:
                for inherited in self.class_mro(target, seen):
                    if inherited not in result:
                        result.append(inherited)
        if root:
            self._class_mros[owner] = result
        return result

    def member_binding(self, owner: ast.ClassDef, name: str) -> Binding | None:
        key = (owner, name)
        if key in self._member_bindings:
            return self._member_bindings[key]
        for base in self.class_mro(owner, set()):
            binding = next(
                (binding for binding in self.node_bindings(base) if binding.name == name),
                None,
            )
            if binding is not None:
                self._member_bindings[key] = binding
                return binding
        self._member_bindings[key] = None
        return None

    @staticmethod
    def is_property(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for decorator in function.decorator_list:
            name = decorator.attr if isinstance(decorator, ast.Attribute) else decorator
            if isinstance(name, ast.Name) and name.id in {"property", "cached_property"}:
                return True
        return False

    def class_hint(self, owner: ast.ClassDef) -> TypeHint:
        path = owner.__dict__.get("annotation_path")
        return TypeHint((path,)) if isinstance(path, str) else TypeHint()

    def canonical_hint(self, hint: TypeHint, seen: frozenset[str] = frozenset()) -> TypeHint:
        """Follow syntax-only re-exports and aliases, including nested shapes."""
        if not seen and hint in self.canonical_hints:
            return self.canonical_hints[hint]
        arguments = tuple(self.canonical_hint(argument, seen) for argument in hint.arguments)
        result = replace(hint, arguments=arguments)
        if hint.origin != "parameter" and hint.classes:
            alternatives: list[TypeHint] = []
            for path in hint.classes:
                alias = self.annotation_aliases.get(path)
                if path in self.annotation_classes or alias is None:
                    expanded = TypeHint((path,))
                elif path in seen:
                    expanded = TypeHint(incomplete=True)
                else:
                    expanded = self.canonical_hint(alias, seen | {path})
                    if not expanded:
                        expanded = replace(expanded, incomplete=True)
                alternatives.append(replace(
                    expanded,
                    self_reference=hint.self_reference or expanded.self_reference,
                    origin=hint.origin or expanded.origin,
                    arguments=arguments or expanded.arguments,
                    variadic=hint.variadic or expanded.variadic,
                    incomplete=hint.incomplete or expanded.incomplete,
                ))
            result = merge_hints(*alternatives)
        if not seen:
            self.canonical_hints[hint] = result
        return result

    def resolved_hint(self, hint: TypeHint, owner: ast.ClassDef | None) -> TypeHint:
        """Resolve a ``Self`` marker relative to the class that declared it."""
        hint = self.canonical_hint(hint)
        if hint.arguments:
            hint = replace(hint, arguments=tuple(self.resolved_hint(argument, owner) for argument in hint.arguments))
        if not hint.self_reference:
            return hint
        classes = hint.classes
        if owner is not None:
            path = owner.__dict__.get("annotation_path")
            if isinstance(path, str):
                classes = tuple(dict.fromkeys((path, *classes)))
        return TypeHint(
            classes,
            origin=hint.origin,
            arguments=hint.arguments,
            variadic=hint.variadic,
            incomplete=hint.incomplete,
        )

    def definition_hint(self, definition: ast.AST, seen: set[int], owner: ast.ClassDef) -> TypeHint:
        if isinstance(definition, ast.ClassDef):
            return self.class_hint(definition)
        if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not self.is_property(definition):
                if definition.decorator_list and self.descriptor(definition) not in {"staticmethod", "classmethod"}:
                    return TypeHint()
                hint = definition.__dict__.get("return_type")
                if not isinstance(hint, TypeHint):
                    return TypeHint()
                result = self.resolved_hint(hint, owner)
                if isinstance(definition, ast.AsyncFunctionDef):
                    result = TypeHint(origin="awaitable", arguments=(result,))
                return TypeHint(origin="callable", arguments=(result,))
            hint = definition.__dict__.get("return_type")
            return self.resolved_hint(hint, owner) if isinstance(hint, TypeHint) else TypeHint()
        if isinstance(definition, ast.Name) and isinstance(definition.ctx, ast.Store):
            hint = definition.__dict__.get("type_annotation")
            if isinstance(hint, TypeHint):
                return self.resolved_hint(hint, owner)
            return self.destructured_hint(definition, seen)
        return TypeHint()

    def member_hint(self, owner: ast.ClassDef, name: str, seen: set[int], receiver: TypeHint | None = None) -> TypeHint:
        result = TypeHint()
        for base in self.class_mro(owner, set()):
            inherited = TypeHint()
            fields = cast(object, base.__dict__.get("field_types"))
            if isinstance(fields, dict):
                typed_fields = cast(dict[str, object], fields)
                hint = typed_fields.get(name)
                if isinstance(hint, TypeHint):
                    inherited = merge_hints(inherited, self.resolved_hint(hint, base))
            binding = next(
                (binding for binding in self.node_bindings(base) if binding.name == name),
                None,
            )
            if binding is not None:
                for definition in self.definitions(binding):
                    inherited = merge_hints(inherited, self.definition_hint(definition, seen, base))
            for assignment in self.instance_assignments.get((id(base), name), []):
                hint = assignment.__dict__.get("type_annotation")
                if isinstance(hint, TypeHint):
                    inherited = merge_hints(inherited, self.resolved_hint(hint, base))
                if assignment.value is not None:
                    inherited = merge_hints(inherited, self.value_hint(assignment.value, seen))
            if receiver is not None:
                inherited = self.instantiate_member(inherited, base, receiver)
            result = merge_hints(result, inherited)
        return result

    def target_indices(self, definition: ast.AST) -> tuple[list[int], ast.AST] | None:
        indices: list[int] = []
        node: ast.AST = definition
        while True:
            parent = self.parents.get(node)
            if not isinstance(parent, (ast.Tuple, ast.List)):
                return indices, node
            positions = [
                position
                for position, element in enumerate(parent.elts)
                if isinstance(element, ast.Starred)
            ]
            index = next(
                (position for position, element in enumerate(parent.elts) if element is node), None
            )
            if index is None or len(positions) > 1:
                return None
            if positions:
                if index == positions[0]:
                    return None
                # Names after ``*rest`` are addressable from the end.
                if index > positions[0]:
                    index -= len(parent.elts)
            indices.insert(0, index)
            node = parent

    def enter_hint(self, context: ast.expr, seen: set[int], *, asynchronous: bool) -> TypeHint:
        method = "__aenter__" if asynchronous else "__enter__"
        context_hint = self.value_hint(context, seen)
        if context_hint.origin == ("async_contextmanager" if asynchronous else "contextmanager"):
            return context_hint.element()
        entered = self.protocol_hint(context_hint, method)
        return entered.element() if asynchronous and entered.origin == "awaitable" else entered

    def protocol_hint(self, receiver: TypeHint, method: str) -> TypeHint:
        """Read a visible special method's result without executing user code."""
        if receiver.incomplete:
            return TypeHint(incomplete=True)
        owners = self.hint_types(receiver, None)
        if not owners or len(owners) != len(receiver.classes):
            return TypeHint(incomplete=True)
        hints: list[TypeHint] = []
        for owner in owners:
            binding = self.member_binding(owner, method)
            if binding is None:
                hints.append(TypeHint(incomplete=True))
                continue
            for definition in self.definitions(binding):
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    hint = definition.__dict__.get("return_type")
                    if isinstance(hint, TypeHint) and (not definition.decorator_list or self.descriptor(definition) is not None):
                        declaring = self.parents.get(definition)
                        result = self.instantiate_member(self.resolved_hint(hint, owner), declaring if isinstance(declaring, ast.ClassDef) else owner, receiver)
                        if hint.self_reference and not hint.origin and not hint.arguments:
                            result = merge_hints(result, receiver)
                        hints.append(TypeHint(origin="awaitable", arguments=(result,)) if isinstance(definition, ast.AsyncFunctionDef) else result)
                    else:
                        hints.append(TypeHint(incomplete=True))
                else:
                    hints.append(TypeHint(incomplete=True))
        return join_hints(hints)

    def iterated_hint(self, receiver: TypeHint, *, asynchronous: bool = False) -> TypeHint:
        """Infer iteration structurally, including ordinary generic containers."""
        if receiver.origin in {"sequence", "mapping", "tuple"}:
            return receiver.iterated()
        iterator = self.protocol_hint(receiver, "__aiter__" if asynchronous else "__iter__")
        if iterator.origin in {"sequence", "mapping", "tuple"}:
            return iterator.iterated()
        result = self.protocol_hint(iterator, "__anext__" if asynchronous else "__next__")
        if asynchronous and result.origin == "awaitable":
            return result.element()
        return result

    def container_hint(self, container: ast.AST, seen: set[int]) -> TypeHint:
        """The annotation type produced by the source of a store target container."""
        parent = self.parents.get(container)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            if parent.value is None or (
                isinstance(parent, ast.Assign) and container not in parent.targets
            ):
                return TypeHint()
            return self.value_hint(parent.value, seen)
        if isinstance(parent, (ast.For, ast.AsyncFor)) and parent.target is container:
            return self.iterated_hint(self.value_hint(parent.iter, seen), asynchronous=isinstance(parent, ast.AsyncFor))
        if isinstance(parent, ast.withitem):
            statement = self.parents.get(parent)
            if (
                not isinstance(statement, (ast.With, ast.AsyncWith))
                or parent.optional_vars is not container
            ):
                return TypeHint()
            return self.enter_hint(
                parent.context_expr, seen, asynchronous=isinstance(statement, ast.AsyncWith)
            )
        if isinstance(parent, (ast.With, ast.AsyncWith)):
            item = next((item for item in parent.items if item.optional_vars is container), None)
            return (
                self.enter_hint(
                    item.context_expr, seen, asynchronous=isinstance(parent, ast.AsyncWith)
                )
                if item is not None
                else TypeHint()
            )
        if isinstance(parent, ast.NamedExpr) and parent.target is container:
            return self.value_hint(parent.value, seen)
        if isinstance(parent, ast.comprehension) and parent.target is container:
            return self.iterated_hint(self.value_hint(parent.iter, seen), asynchronous=bool(parent.is_async))
        return TypeHint()

    def destructured_hint(self, definition: ast.AST, seen: set[int]) -> TypeHint:
        """Infer a store target from assignment, iteration, or a context manager."""
        star = self.parents.get(definition)
        if isinstance(star, ast.Starred) and star.value is definition:
            container = self.parents.get(star)
            if isinstance(container, (ast.Tuple, ast.List)):
                source = self.container_hint(container, seen)
                position = next(
                    index for index, element in enumerate(container.elts) if element is star
                )
                trailing = len(container.elts) - position - 1
                if source.origin == "tuple":
                    if source.variadic:
                        element = source.arguments[-1] if source.arguments else TypeHint()
                    else:
                        end = len(source.arguments) - trailing
                        element = merge_hints(*source.arguments[position:end])
                elif source.origin in {"sequence", "mapping"}:
                    element = source.element()
                else:
                    element = TypeHint()
                if element:
                    return TypeHint(origin="sequence", arguments=(element,))
            return TypeHint()
        located = self.target_indices(definition)
        if located is None:
            return TypeHint()
        indices, container = located
        hint = self.container_hint(container, seen)
        for index in indices:
            if index < 0:
                position = len(hint.arguments) + index
                if (
                    hint.origin == "tuple"
                    and not hint.variadic
                    and 0 <= position < len(hint.arguments)
                ):
                    hint = hint.arguments[position]
                    continue
                element = hint.element()
                if not element:
                    return TypeHint()
                hint = element
                continue
            unpacked = hint.unpacked()
            if index < len(unpacked):
                hint = unpacked[index]
                continue
            if hint.origin == "tuple" and hint.variadic and hint.arguments:
                hint = hint.arguments[-1]
                continue
            return TypeHint()
        return hint

    def incoming_definitions(self, node: ast.Name, definitions: list[ast.AST]) -> list[ast.AST]:
        """A self-rebinding's RHS reads the previous value, not its own store."""
        child: ast.AST = node
        parent = self.parents.get(child)
        while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is child:
                scope = self.parents.get(parent)
                if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return definitions
                positions = {statement: index for index, statement in enumerate(scope.body)}
                if parent not in positions or not any(self.parents.get(definition) is parent for definition in definitions):
                    return definitions
                prior: list[ast.AST] = []
                for definition in definitions:
                    statement = self.parents.get(definition)
                    if isinstance(definition, ast.arg) and statement is scope.args:
                        prior.append(definition)
                    elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement in positions:
                        if positions[statement] < positions[parent]:
                            prior.append(definition)
                    else:
                        return definitions
                return prior or definitions
            child, parent = parent, self.parents.get(parent)
        return definitions

    def name_hint(self, node: ast.Name, seen: set[int]) -> TypeHint:
        binding = self.bindings.get(node)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) > 1:
            definitions = self.incoming_definitions(node, definitions)
        if len(definitions) > 1:
            hints: list[TypeHint] = []
            declared = merge_hints(*(
                hint for definition in definitions
                if isinstance(hint := definition.__dict__.get("type_annotation"), TypeHint)
            ))
            for definition in definitions:
                if not isinstance(definition, (ast.Name, ast.arg)) or (
                    isinstance(definition, ast.Name) and not isinstance(definition.ctx, ast.Store)
                ):
                    return TypeHint()
                hint = definition.__dict__.get("type_annotation")
                inferred = (
                    self.resolved_hint(hint, self.enclosing_class(definition))
                    if isinstance(hint, TypeHint)
                    else self.destructured_hint(definition, seen)
                )
                # A declaration can describe an imported factory whose native
                # implementation has no return annotation. Do not extend that
                # contract to opaque callbacks or arbitrary local rebindings.
                if not inferred and isinstance(definition, ast.Name):
                    assignment = self.parents.get(definition)
                    if isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                        value = assignment.value
                        if (
                            isinstance(value, ast.Constant) and value.value is None
                        ) or (
                            isinstance(value, ast.Call) and self.imported_path(value.func) is not None
                        ):
                            inferred = declared
                if not inferred:
                    return TypeHint()
                hints.append(inferred)
            return merge_hints(*hints)
        if len(definitions) == 1:
            definition = definitions[0]
            if isinstance(definition, ast.arg):
                hint = definition.__dict__.get("type_annotation")
                if isinstance(hint, TypeHint):
                    function = self.parents.get(self.parents.get(definition, definition))
                    owner = self.parents.get(function) if function is not None else None
                    return self.resolved_hint(
                        hint, owner if isinstance(owner, ast.ClassDef) else None
                    )
            elif isinstance(definition, ast.Name) and isinstance(definition.ctx, ast.Store):
                hint = definition.__dict__.get("type_annotation")
                if isinstance(hint, TypeHint):
                    return self.resolved_hint(hint, self.enclosing_class(definition))
                destructured = self.destructured_hint(definition, seen)
                if destructured:
                    return destructured
            elif isinstance(definition, ast.ClassDef):
                return self.class_hint(definition)
            elif isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hint = definition.__dict__.get("return_type")
                if isinstance(hint, TypeHint):
                    result = TypeHint(origin="awaitable", arguments=(hint,)) if isinstance(definition, ast.AsyncFunctionDef) else hint
                    return TypeHint(origin="callable", arguments=(result,))
        target = self.binding_target(binding, set())
        return self.class_hint(target) if isinstance(target, ast.ClassDef) else TypeHint()

    def attribute_hint(self, node: ast.Attribute, seen: set[int]) -> TypeHint:
        receiver = self.value_hint(node.value, seen)
        owners = self.hint_types(receiver, self.enclosing_class(node))
        if not owners and isinstance(node.value, ast.Name):
            target = self.binding_target(self.bindings.get(node.value), set())
            if isinstance(target, ast.ClassDef):
                owners = {target}
        hints = [self.member_hint(owner, node.attr, seen, receiver) for owner in owners]
        if receiver.incomplete or any(
            path not in self.annotation_classes and path != "types.NoneType" for path in receiver.classes
        ):
            hints.append(TypeHint(incomplete=True))
        result = join_hints(hints)
        if result:
            return result
        owner = self.target(node.value, seen, instances=False)
        if isinstance(owner, ast.ClassDef):
            return self.member_hint(owner, node.attr, seen)
        if isinstance(owner, str):
            target = self.binding_target(self.symbols.get(owner, _EMPTY_SYMBOLS).get(node.attr), seen)
            if isinstance(target, ast.ClassDef):
                return self.class_hint(target)
        return TypeHint()

    def project_receiver(self, receiver: TypeHint, owner: ast.ClassDef, seen: frozenset[str] = frozenset()) -> TypeHint:
        """Project generic arguments through syntax-derived inheritance edges."""
        destination = self.class_hint(owner).classes
        if receiver.classes == destination:
            return receiver
        candidates: list[TypeHint] = []
        for path in receiver.classes:
            if path in seen:
                continue
            source = self.annotation_classes.get(path)
            if source is None:
                continue
            names = cast(tuple[str, ...], source.__dict__.get("type_parameters", ()))
            substitutions = dict(zip(names, receiver.arguments, strict=False))
            bases = cast(tuple[TypeHint, ...], source.__dict__.get("base_types", ()))
            for base in bases:
                projected = self.project_receiver(base.substituted(substitutions), owner, seen | {path})
                if projected:
                    candidates.append(projected)
        if receiver.incomplete:
            candidates.append(TypeHint(incomplete=True))
        return join_hints(candidates)

    def instantiate_member(self, hint: TypeHint, owner: ast.ClassDef, receiver: TypeHint) -> TypeHint:
        parameters = owner.__dict__.get("type_parameters", ())
        if isinstance(parameters, tuple) and parameters:
            names = cast(tuple[str, ...], parameters)
            projected = self.project_receiver(receiver, owner)
            return hint.substituted(dict(zip(names, projected.arguments, strict=False)))
        return hint

    def instantiate_call(self, hint: TypeHint, function: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call, seen: set[int]) -> TypeHint:
        """Unify generic parameter shapes with actual positional/keyword values."""
        declaring = self.parents.get(function)
        if not function.__dict__.get("type_parameters") and not (
            isinstance(declaring, ast.ClassDef) and declaring.__dict__.get("type_parameters")
        ):
            return hint
        substitutions: dict[str, TypeHint] = {}

        bound = isinstance(declaring, ast.ClassDef) and self.descriptor(function) != "staticmethod" and (
            self.descriptor(function) == "classmethod"
            or (isinstance(call.func, ast.Attribute) and not isinstance(self.target(call.func.value, seen, instances=False), ast.ClassDef))
            or self.target(call.func, seen, instances=False) is declaring
        )
        supplied = bind_call(function.args, call, lambda expression: self.value_hint(expression, seen), bound=bound)
        if supplied is None:
            return hint.substituted({})
        for parameter, actual in supplied.items():
            template = parameter.__dict__.get("type_annotation")
            if isinstance(template, TypeHint):
                if template.origin is None and len(template.classes) == 1:
                    expected = self.annotation_classes.get(template.classes[0])
                    if expected is not None:
                        actual = self.project_receiver(actual, expected)
                unify_hint(template, actual, substitutions)
        return hint.substituted(substitutions)

    def call_hint(self, node: ast.Call, seen: set[int]) -> TypeHint:
        binding = self.bindings.get(node.func)
        path = (
            f"builtins.{getattr(binding, 'name', '')}"
            if isinstance(cast(object, binding), BuiltinBinding)
            else self.imported_path(node.func)
        )
        structural = operation_hint(node, path, lambda expression: self.value_hint(expression, seen), self.iterated_hint)
        if structural is not None:
            return structural
        target = self.target(node.func, seen, instances=False)
        if isinstance(target, ast.ClassDef):
            result = self.class_hint(target)
            parameters = cast(tuple[str, ...], target.__dict__.get("type_parameters", ()))
            if parameters:
                result = TypeHint(result.classes, arguments=tuple(TypeHint((name,), origin="parameter") for name in parameters))
                constructor = self.member_binding(target, "__init__")
                definitions = self.definitions(constructor) if constructor is not None else []
                if len(definitions) == 1 and isinstance(definitions[0], ast.FunctionDef):
                    result = self.instantiate_call(result, definitions[0], node, seen)
            return result
        if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hint = target.__dict__.get("return_type")
            declaring = self.parents.get(target)
            if isinstance(hint, TypeHint):
                result = self.resolved_hint(
                    hint, declaring if isinstance(declaring, ast.ClassDef) else None
                )
                result = self.instantiate_call(result, target, node, seen)
                if isinstance(declaring, ast.ClassDef) and isinstance(node.func, ast.Attribute):
                    result = self.instantiate_member(result, declaring, self.value_hint(node.func.value, seen))
                decorators = {self.imported_path(decorator) for decorator in target.decorator_list}
                if decorators & {"contextlib.contextmanager", "contextlib.asynccontextmanager"}:
                    return TypeHint(origin="async_contextmanager" if "contextlib.asynccontextmanager" in decorators else "contextmanager", arguments=(result.iterated(),))
                return TypeHint(origin="awaitable", arguments=(result,)) if isinstance(target, ast.AsyncFunctionDef) else result
            return TypeHint()
        if isinstance(node.func, ast.Attribute):
            receiver = self.value_hint(node.func.value, seen)
            if receiver.origin == "mapping" and node.func.attr in {"keys", "values", "items"} and not node.args and not node.keywords:
                key = receiver.arguments[0] if receiver.arguments else TypeHint()
                value = receiver.element()
                item = TypeHint(origin="tuple", arguments=(key, value)) if node.func.attr == "items" else key if node.func.attr == "keys" else value
                return TypeHint(origin="sequence", arguments=(item,), incomplete=receiver.incomplete)
            if receiver.origin == "mapping" and node.func.attr in {"get", "pop", "setdefault"}:
                element = receiver.element()
                if element:
                    return join_hints([element, self.value_hint(node.args[1], seen)]) if len(node.args) > 1 else element
            if receiver.origin == "sequence" and node.func.attr == "pop":
                element = receiver.element()
                if element:
                    return element
            if (
                node.func.attr == "copy"
                and receiver.origin in {"mapping", "sequence"}
                and not receiver.classes
            ):
                return receiver
        callable_hint = self.value_hint(node.func, seen)
        if callable_hint.origin == "callable" and callable_hint.arguments:
            return callable_hint.arguments[-1]
        callable_owner = self.target(node.func, seen, instances=True)
        if isinstance(callable_owner, ast.ClassDef):
            binding = self.member_binding(callable_owner, "__call__")
            if binding is not None:
                hints = [
                    self.resolved_hint(hint, callable_owner)
                    for definition in self.definitions(binding)
                    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and isinstance(hint := definition.__dict__.get("return_type"), TypeHint)
                ]
                return merge_hints(*hints)
        return TypeHint()

    def value_hint(self, node: ast.AST | None, seen: set[int] | None = None) -> TypeHint:
        """Infer the annotation-derived value type of an expression."""
        if node is None:
            return TypeHint()
        if not seen:
            # Keep independent queries separate from the bounded pool of
            # recursion-context entries, which can fill up during one query.
            hint = self.root_hints.get(node)
            if hint is None:
                hint = self._value_hint(node, {id(node)})
                self.root_hints[node] = hint
            return hint
        if id(node) in seen:
            return TypeHint()
        # Attribute and call resolution repeatedly ask for the same hints.
        # Include the recursion context: a result truncated by a cycle must
        # not be reused for an independent traversal of the same expression.
        key = (node, frozenset(seen))
        cached = self.hint_cache.get(key)
        if cached is not None:
            return cached
        hint = self._value_hint(node, {*key[1], id(node)})
        if len(self.hint_cache) >= 32768:
            self.hint_cache.clear()
        self.hint_cache[key] = hint
        return hint

    def _value_hint(self, node: ast.AST, seen: set[int]) -> TypeHint:
        explicit = node.__dict__.get("expression_type")
        if isinstance(explicit, TypeHint):
            return self.resolved_hint(explicit, self.enclosing_class(node))
        if isinstance(node, ast.Name):
            return self.name_hint(node, seen)
        if isinstance(node, ast.Attribute):
            return self.attribute_hint(node, seen)
        if isinstance(node, ast.Call):
            return self.call_hint(node, seen)
        if isinstance(node, ast.Await):
            hint = self.value_hint(node.value, seen)
            return hint.element() if hint.origin == "awaitable" else hint
        if isinstance(node, (ast.NamedExpr, ast.Starred)):
            return self.value_hint(node.value, seen)
        if isinstance(node, ast.Subscript):
            base_hint = self.value_hint(node.value, seen)
            if isinstance(node.slice, ast.Slice):
                if base_hint.origin == "tuple" and not base_hint.variadic:
                    parts = (node.slice.lower, node.slice.upper, node.slice.step)
                    if all(part is None or (isinstance(part, ast.Constant) and isinstance(part.value, int)) for part in parts):
                        bounds = [part.value if isinstance(part, ast.Constant) and isinstance(part.value, int) else None for part in parts]
                        if bounds[2] != 0:
                            return TypeHint(origin="tuple", arguments=base_hint.arguments[slice(*bounds)], incomplete=base_hint.incomplete)
                return base_hint
            index = (
                node.slice.value
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)
                else None
            )
            element = base_hint.element(index)
            if base_hint.origin in {"sequence", "mapping", "tuple", "awaitable"}:
                return element
            indexed = self.protocol_hint(base_hint, "__getitem__")
            return indexed or element
        if isinstance(node, ast.IfExp):
            return join_hints([self.value_hint(node.body, seen), self.value_hint(node.orelse, seen)])
        if isinstance(node, ast.BoolOp):
            return join_hints([self.value_hint(value, seen) for value in node.values])
        if isinstance(node, ast.Tuple):
            return TypeHint(
                origin="tuple",
                arguments=tuple(self.value_hint(element, seen) for element in node.elts),
            )
        if isinstance(node, (ast.List, ast.Set)):
            return TypeHint(
                origin="sequence",
                arguments=(
                    join_hints([
                        self.iterated_hint(self.value_hint(element.value, seen))
                        if isinstance(element, ast.Starred)
                        else self.value_hint(element, seen)
                        for element in node.elts
                    ]),
                ),
            )
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return TypeHint(
                origin="sequence",
                arguments=(self.value_hint(node.elt, seen),),
            )
        if isinstance(node, ast.DictComp):
            return TypeHint(
                origin="mapping",
                arguments=(self.value_hint(node.key, seen), self.value_hint(node.value, seen)),
            )
        if isinstance(node, ast.Dict):
            key_hints: list[TypeHint] = []
            value_hints: list[TypeHint] = []
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    expanded = self.value_hint(value, seen)
                    key_hints.append(expanded.iterated() if expanded.origin == "mapping" else TypeHint(incomplete=True))
                    value_hints.append(expanded.element() if expanded.origin == "mapping" else TypeHint(incomplete=True))
                else:
                    key_hints.append(self.value_hint(key, seen))
                    value_hints.append(self.value_hint(value, seen))
            keys = join_hints(key_hints)
            values = join_hints(value_hints)
            return TypeHint(origin="mapping", arguments=(keys, values))
        return TypeHint()

    def type_targets(self, node: ast.AST, seen: set[int] | None = None) -> set[ast.ClassDef]:
        """Infer the internal classes an expression's value can belong to."""
        return self.hint_types(self.value_hint(node, seen), self.enclosing_class(node))

    def target(
        self, node: ast.AST, seen: set[int] | None = None, *, instances: bool = True
    ) -> ast.AST | str | None:
        seen = set() if seen is None else seen
        if isinstance(node, ast.Name):
            target = self.binding_target(self.bindings.get(node), seen, instances=instances)
            if target is not None or not instances:
                return target
            inferred = self.type_targets(node, seen)
            return next(iter(inferred)) if len(inferred) == 1 else None
        if isinstance(node, ast.Lambda):
            return node
        if isinstance(node, ast.Attribute):
            receiver_hint = self.value_hint(node.value, seen)
            if receiver_hint.incomplete:
                return None
            owners = self.hint_types(receiver_hint, self.enclosing_class(node.value))
            if owners:
                targets: list[ast.AST | str] = []
                for inferred_owner in owners:
                    binding = self.member_binding(inferred_owner, node.attr)
                    if binding is None:
                        continue
                    target = self.binding_target(binding, seen, instances=instances)
                    if target is not None and target not in targets:
                        targets.append(target)
                if len(targets) == 1:
                    return targets[0]
            receiver_target = self.target(node.value, seen, instances=instances)
            if isinstance(receiver_target, str):
                return self.binding_target(
                    self.symbols.get(receiver_target, _EMPTY_SYMBOLS).get(node.attr),
                    seen,
                    instances=instances,
                )
            if isinstance(receiver_target, ast.ClassDef):
                binding = self.member_binding(receiver_target, node.attr)
                return self.binding_target(binding, seen, instances=instances)
        elif instances and isinstance(node, ast.Call):
            # Calling an inferred instance invokes __call__, not its class's
            # constructor, and may return an entirely different receiver.
            call_target = self.target(node.func, seen, instances=False)
            if isinstance(call_target, ast.ClassDef):
                return call_target
        return None

    def fresh_name(self) -> str:
        # The caller reserves lexical names and keyword keys for this scope.
        while True:
            number = self.next_name
            self.next_name += 1
            name = ""
            while True:
                name = chr(ord("a") + number % 26) + name
                number = number // 26 - 1
                if number < 0:
                    break
            if name not in self.used:
                self.used.add(name)
                return name

    def exposed_functions(self, module: str, seen: set[str] | None = None) -> set[Function]:
        if seen is None:
            seen = set()
        if module in seen:
            return set()
        seen.add(module)
        exposed = {function for function in self.functions if self.modules[function] == module}
        for binding in self.symbols.get(module, _EMPTY_SYMBOLS).values():
            target = self.binding_target(binding, set())
            if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                exposed.add(target)
            elif isinstance(target, ast.ClassDef):
                exposed.update(
                    child
                    for child in ast.walk(target)
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                )
            elif isinstance(target, str):
                exposed.update(self.exposed_functions(target, seen))
        return exposed

    def analyze_calls(self) -> tuple[dict[Function, list[ast.Call]], set[Function], set[Function]]:
        """Find closed callers and distinguish dynamic keywords from reflection."""
        calls: dict[Function, list[ast.Call]] = {function: [] for function in self.functions}
        blocked: set[Function] = set()
        reflected: set[Function] = set()
        by_name: dict[str, list[Function]] = {}
        methods_by_name: dict[str, list[Function]] = {}
        if self.frame_inspection:
            # Stack/frame APIs can inspect any active scope, not just a known
            # function object. There is no safe cross-module rename in that case.
            reflected.update(self.functions)
        for module, tree in self.trees.items():
            if getattr(tree, "tainted", False):
                exposed = self.exposed_functions(module)
                blocked.update(exposed)
                reflected.update(exposed)
        for module in self.star_imports:
            exposed = self.exposed_functions(module)
            blocked.update(exposed)
            reflected.update(exposed)
        for function in self.functions:
            if isinstance(function, ast.Lambda):
                parent = self.parents.get(function)
                if not (
                    (isinstance(parent, ast.Call) and parent.func is function)
                    or self.alias_assignment(function) is not None
                ):
                    reflected.add(function)
            else:
                by_name.setdefault(function.name, []).append(function)
                owner = self.parents.get(function)
                if isinstance(owner, ast.ClassDef):
                    methods_by_name.setdefault(function.name, []).append(function)
                descriptor = self.descriptor(function)
                if (
                    function.decorator_list
                    and descriptor is None
                    and not self.forwarding_decorators(function)
                    and not self.is_property(function)
                ) or function.name in self.strings:
                    blocked.add(function)
                    reflected.add(function)
                if isinstance(owner, ast.ClassDef) and (
                    owner.bases
                    or not self.safe_class_decorators(owner)
                    or owner.keywords
                    or (function.name.startswith("__") and function.name != "__init__")
                    or (
                        descriptor != "staticmethod"
                        and (
                            not (positional := function.args.posonlyargs + function.args.args)
                            or positional[0].arg not in {"self", "cls"}
                        )
                    )
                    or any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name in {"__new__", "__getattr__", "__getattribute__"}
                        for child in owner.body
                    )
                ):
                    blocked.add(function)
                    reflected.add(function)
        for alias, (_, name) in self.imports.items():
            if name and self.binding_target(self.bindings.get(alias), set()) is None:
                # An unresolved aliased import must not hide a possible caller
                # merely because its local name differs from the definition.
                blocked.update(by_name.get(name, []))
                reflected.update(by_name.get(name, []))
        for nodes in self.module_nodes.values():
            for node in nodes:
                if id(node) in self.annotation_nodes:
                    continue
                if isinstance(node, ast.Call):
                    target = self.target(node.func)
                    if (
                        isinstance(target, ast.ClassDef)
                        and self.target(node.func, instances=False) is target
                    ):
                        # Ordinary constructors forward their keywords to __init__.
                        target = next(
                            (
                                child
                                for child in target.body
                                if isinstance(child, ast.FunctionDef) and child.name == "__init__"
                            ),
                            None,
                        )
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        calls[target].append(node)
                        if any(
                            arg.arg is None and self.literal_keywords(arg) is None
                            for arg in node.keywords
                        ):
                            blocked.add(target)
                elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                    target = self.target(node)
                    parent = self.parents.get(node)
                    direct_call = isinstance(parent, ast.Call) and parent.func is node
                    if self.alias_assignment(node) is not None:
                        # Follow all uses of the destination instead of treating
                        # the assignment itself as an escape.
                        continue
                    use = self.parents.get(parent) if direct_call and parent else parent
                    if isinstance(use, ast.keyword):
                        # Keyword arguments wrap their value; inspect the call.
                        use = self.parents.get(use)
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        if not direct_call and not (
                            isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and self.is_property(target)
                        ):
                            blocked.add(target)
                            reflected.add(target)
                    elif isinstance(node, ast.Attribute):
                        # A receiver whose type is unknown might invoke any
                        # same-named implementation. Do not guess from spelling.
                        # Free functions with that name are not methods; their
                        # escaping references/modules are checked separately.
                        blocked.update(methods_by_name.get(node.attr, []))
                        reflected.update(methods_by_name.get(node.attr, []))
                    # An unresolved lexical name does not refer to every
                    # same-spelled function in unrelated scopes. Actual aliases
                    # and callable arguments escape at their original reference.
                    if isinstance(target, ast.ClassDef):
                        receiver = parent if direct_call else node
                        # A single-assignment instance remains closed when all
                        # of its later uses are resolved method calls. Those
                        # references are checked separately by this same walk;
                        # passing/storing/reflecting on it still blocks methods.
                        if (
                            direct_call
                            and isinstance(use, (ast.Assign, ast.AnnAssign))
                            and use.value is receiver
                        ):
                            assigned = use.targets if isinstance(use, ast.Assign) else [use.target]
                            if (
                                len(assigned) == 1
                                and isinstance(assigned[0], ast.Name)
                                and self.target(assigned[0]) is target
                            ):
                                continue
                        literal_use = isinstance(
                            use,
                            (
                                ast.Compare,
                                ast.BoolOp,
                                ast.UnaryOp,
                                ast.If,
                                ast.IfExp,
                                ast.Assert,
                                ast.While,
                            ),
                        ) or (
                            direct_call
                            and isinstance(
                                use,
                                (ast.Return, ast.Expr, ast.Tuple, ast.List, ast.Set, ast.Dict),
                            )
                        )
                        argument_use = (
                            direct_call
                            and isinstance(use, ast.Call)
                            and (
                                receiver in use.args
                                or any(keyword.value is receiver for keyword in use.keywords)
                            )
                            and self.callee_accepts_argument(use, target)
                        )
                        storage_use = (
                            use is not None
                            and receiver is not None
                            and self.stores_instance(use, receiver)
                        )
                        return_use = (
                            use is not None
                            and receiver is not None
                            and self.returns_instance(use, receiver, target)
                        )
                        factory_use = (
                            isinstance(use, ast.Call)
                            and receiver is not None
                            and (
                                receiver in use.args
                                or any(keyword.value is receiver for keyword in use.keywords)
                            )
                            and self.dataclasses_field_call(use)
                        )
                        if (
                            not (
                                isinstance(use, ast.Attribute)
                                and use.value is receiver
                                # Reading ordinary instance/class data does not
                                # expose every method signature. Dunder attributes
                                # such as __dict__ and __class__ still do.
                                and not use.attr.startswith("__")
                            )
                            and not literal_use
                            and not argument_use
                            and not storage_use
                            and not return_use
                            and not factory_use
                        ):
                            methods = [
                                child
                                for child in target.body
                                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                            ]
                            blocked.update(methods)
                            reflected.update(methods)
                    elif isinstance(target, str) and not (
                        isinstance(parent, ast.Attribute)
                        and (
                            self.target(parent) is not None
                            or self.imported_attribute(target, parent)
                        )
                    ):
                        # Passing a module to getattr/vars or reading __dict__
                        # exposes signatures without statically visible callers.
                        exposed = self.exposed_functions(target)
                        blocked.update(exposed)
                        reflected.update(exposed)
                elif isinstance(node, ast.Attribute):
                    blocked.update(methods_by_name.get(node.attr, []))
                    reflected.update(methods_by_name.get(node.attr, []))
        return calls, blocked, reflected

    def run(self) -> None:
        self._run(None)

    def _run(self, call_analysis: CallAnalysis | None) -> None:
        self._definitions.clear()
        self._class_mros.clear()
        self._member_bindings.clear()
        self.root_hints.clear()
        self.hint_cache.clear()
        self.imported_paths.clear()
        self.renamed = 0
        self.skipped.clear()
        calls, blocked, reflected = call_analysis or self.analyze_calls()
        for function in self.functions:
            tree = self.trees[self.modules[function]]
            parameters = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
            variadics = [arg for arg in (function.args.vararg, function.args.kwarg) if arg]
            if getattr(tree, "tainted", False):
                self.skipped["dynamic namespace"] += len(parameters) + len(variadics)
                continue
            # Reuse short names in independent scopes. Reserve every name in
            # this subtree (including closures), and every caller keyword, so
            # neither lexical capture nor accidental **kwargs capture is possible.
            reserved = False
            keyword_references: dict[str, list[ast.keyword | ast.Constant]] = {}
            decorated = (
                not isinstance(function, ast.Lambda)
                and bool(function.decorator_list)
                and self.descriptor(function) is None
                and not self.forwarding_decorators(function)
            )
            for argument in parameters + variadics:
                old = argument.arg
                # A matching string alone is not reflection. Dynamic **kwargs
                # and escaped/reflected callables already block the signature.
                reason = None
                owner = self.parents.get(function)
                if argument.__dict__.get("no_rename"):
                    reason = "no_rename_arg decorator"
                elif old.startswith("__"):
                    reason = "name-mangled/special parameter"
                elif decorated:
                    reason = "unknown decorator"
                elif function in reflected:
                    reason = (
                        "inheritance/decorated class"
                        if isinstance(owner, ast.ClassDef)
                        and (owner.bases or owner.decorator_list or owner.keywords)
                        else "escaping/reflected callable or receiver"
                    )
                elif argument in parameters and function in blocked:
                    reason = "dynamic keywords/unresolved caller"
                elif argument in parameters and not calls[function]:
                    reason = "no resolved calls"
                if reason is not None:
                    self.skipped[reason] += 1
                    continue
                binding = self.bindings.get(argument)
                if binding is None or any(
                    isinstance(ref, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for ref in binding.references
                ):
                    self.skipped["ambiguous lexical binding"] += 1
                    continue
                if not reserved:
                    # Most preserved APIs never need a fresh name. Do not walk
                    # their bodies or inspect their caller dictionaries at all.
                    self.used = set(_RESERVED_NAMES)
                    self.next_name = 0
                    for node in ast.walk(function):
                        self.used.update(binding.name for binding in self.node_bindings(node))
                        if isinstance(node, ast.Name):
                            self.used.add(node.id)
                        elif isinstance(node, ast.arg):
                            self.used.add(node.arg)
                        elif isinstance(node, (ast.Global, ast.Nonlocal)):
                            self.used.update(node.names)
                    for call in calls[function]:
                        for call_keyword in call.keywords:
                            if call_keyword.arg is not None:
                                self.used.add(call_keyword.arg)
                                keyword_references.setdefault(call_keyword.arg, []).append(call_keyword)
                            else:
                                for key in self.literal_keywords(call_keyword) or ():
                                    spelling = cast(str, key.value)
                                    self.used.add(spelling)
                                    keyword_references.setdefault(spelling, []).append(key)
                    reserved = True
                new = self.fresh_name()
                # NameBinding.rename normally keeps keyword-capable arg names
                # and inserts aliases. We rewrite verified callers instead.
                binding.references.remove(argument)
                binding.rename(new)
                binding.references.append(argument)
                argument.arg = new
                self.renamed += 1
                if argument in parameters:
                    for reference in keyword_references.get(old, ()):
                        if isinstance(reference, ast.keyword):
                            reference.arg = new
                        else:
                            reference.value = new

    @staticmethod
    def literal_keywords(argument: ast.keyword) -> list[ast.Constant] | None:
        """Only inline, fully known dictionaries can have their keys rewritten."""
        if not isinstance(argument.value, ast.Dict):
            return None
        keys = argument.value.keys
        if all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keys):
            return cast(list[ast.Constant], keys)
        return None

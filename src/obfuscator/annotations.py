from __future__ import annotations

import ast
from dataclasses import dataclass

__all__: list[str] = ["AnnotationIndex", "TypeHint", "merge_hints"]

# Container annotations are resolved structurally so a chained expression can
# follow the type of an indexed/iterated/awaited value, not just its spelling.
_SEQUENCE_NAMES: frozenset[str] = frozenset(
    {
        "AbstractSet",
        "AsyncGenerator",
        "AsyncIterable",
        "AsyncIterator",
        "Collection",
        "Container",
        "Deque",
        "FrozenSet",
        "Generator",
        "Iterable",
        "Iterator",
        "List",
        "MutableSequence",
        "MutableSet",
        "Reversible",
        "Sequence",
        "Set",
        "deque",
        "frozenset",
        "list",
        "set",
    }
)
_MAPPING_NAMES: frozenset[str] = frozenset(
    {
        "ChainMap",
        "DefaultDict",
        "Dict",
        "Mapping",
        "MutableMapping",
        "OrderedDict",
        "dict",
    }
)
_TUPLE_NAMES: frozenset[str] = frozenset({"Tuple", "tuple"})
_AWAITABLE_NAMES: frozenset[str] = frozenset({"Awaitable", "Coroutine", "Future", "Task"})
_WRAPPER_NAMES: frozenset[str] = frozenset(
    {
        "Annotated",
        "ClassVar",
        "Final",
        "NotRequired",
        "ReadOnly",
        "Required",
    }
)


@dataclass(frozen=True)
class TypeHint:
    """An annotation reduced to internal classes and container structure."""

    classes: tuple[str, ...] = ()
    self_reference: bool = False
    origin: str | None = None
    arguments: tuple[TypeHint, ...] = ()
    variadic: bool = False

    def __bool__(self) -> bool:
        return bool(self.classes or self.self_reference or self.origin or self.arguments)

    def element(self, index: int | None = None) -> TypeHint:
        """The value type produced by indexing/iterating this annotation."""
        if self.origin == "sequence":
            return self.arguments[0] if self.arguments else TypeHint()
        if self.origin == "mapping":
            return self.arguments[-1] if len(self.arguments) > 1 else TypeHint()
        if self.origin == "awaitable":
            return self.arguments[-1] if self.arguments else TypeHint()
        if self.origin == "tuple":
            if index is None:
                return _merge_many(self.arguments)
            if -len(self.arguments) <= index < len(self.arguments):
                return self.arguments[index]
            if self.variadic and self.arguments:
                return self.arguments[-1]
        return TypeHint()

    def unpacked(self) -> tuple[TypeHint, ...]:
        """Element hints for a destructuring assignment."""
        if self.origin == "tuple":
            if self.variadic and self.arguments:
                return (*self.arguments[:-1], self.arguments[-1])
            return self.arguments
        if self.origin in {"sequence", "mapping", "awaitable"}:
            element = self.element()
            return (element,) if element else ()
        return ()

    def iterated(self) -> TypeHint:
        """The value type produced by iterating this annotation."""
        if self.origin in {"sequence", "mapping"}:
            return self.arguments[0] if self.arguments else TypeHint()
        if self.origin == "tuple":
            return _merge_many(self.arguments)
        return TypeHint()


def _merge_many(hints: tuple[TypeHint, ...] | list[TypeHint]) -> TypeHint:
    result = TypeHint()
    for hint in hints:
        result = _merge(result, hint)
    return result


def merge_hints(*hints: TypeHint) -> TypeHint:
    """Merge value types from alternative expressions or declarations."""
    return _merge_many(list(hints))


def _merge(left: TypeHint, right: TypeHint) -> TypeHint:
    if not left:
        return right
    if not right:
        return left
    classes = tuple(dict.fromkeys((*left.classes, *right.classes)))
    self_reference = left.self_reference or right.self_reference
    if left.origin is not None and left.origin == right.origin:
        if left.origin == "tuple" and (
            left.variadic or right.variadic or len(left.arguments) != len(right.arguments)
        ):
            return TypeHint(
                classes,
                self_reference,
                "tuple",
                tuple(dict.fromkeys((*left.arguments, *right.arguments))),
                left.variadic or right.variadic,
            )
        arguments = tuple(map(_merge, left.arguments, right.arguments, strict=False))
        return TypeHint(classes, self_reference, left.origin, arguments, left.variadic)
    if left.origin is not None and right.origin is not None:
        # Different containers have no common value shape; retain only classes.
        return TypeHint(classes, self_reference)
    holder = left if left.origin is not None else right
    return TypeHint(classes, self_reference, holder.origin, holder.arguments, holder.variadic)


class AnnotationIndex:
    """Capture annotation-derived types before release cleanup removes them."""

    def __init__(self, module: str, tree: ast.Module, *, is_package: bool = False) -> None:
        self.module = module
        self.tree = tree
        self.is_package = is_package
        self.names: dict[str, str] = {}
        self.class_paths: set[str] = set()
        self._collect_names(tree.body)

    def apply(self) -> None:
        self._assign_class_paths()
        self._attach_hints(self.tree, None, None, direct_class_body=False)

    def _collect_names(self, statements: list[ast.stmt]) -> None:
        # Register definitions before resolving aliases so an alias may refer to
        # a class declared later in the module.
        for statement in statements:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.names.setdefault(statement.name, f"{self.module}.{statement.name}")
        for statement in statements:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(statement, ast.TypeAlias):
                path = self._path(statement.value)
                if path is not None:
                    self.names.setdefault(statement.name.id, path)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.asname:
                        self.names[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".", maxsplit=1)[0]
                        self.names[root] = root
            elif isinstance(statement, ast.ImportFrom):
                if statement.module == "__future__":
                    continue
                target = self._import_target(statement)
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    self.names[alias.asname or alias.name] = (
                        f"{target}.{alias.name}" if target else alias.name
                    )
            elif isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    continue
                path = self._path(statement.value)
                if path is not None:
                    self.names.setdefault(statement.targets[0].id, path)
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                if isinstance(statement.target, ast.Name):
                    path = self._path(statement.value)
                    if path is not None:
                        self.names.setdefault(statement.target.id, path)
            elif isinstance(statement, ast.If):
                self._collect_names(statement.body)
                self._collect_names(statement.orelse)
            elif isinstance(statement, ast.Try):
                self._collect_names(statement.body)
                for handler in statement.handlers:
                    self._collect_names(handler.body)
                self._collect_names(statement.orelse)
                self._collect_names(statement.finalbody)

    def _import_target(self, node: ast.ImportFrom) -> str:
        package = self.module.split(".")[:-1]
        if self.is_package:
            package = self.module.split(".")
        if node.level:
            package = package[: len(package) - node.level + 1]
            return ".".join([*package, *([node.module] if node.module else [])])
        return node.module or ""

    def _assign_class_paths(self) -> None:
        def walk(node: ast.AST, parent_path: str | None) -> None:
            if isinstance(node, ast.ClassDef):
                path = f"{parent_path}.{node.name}" if parent_path else f"{self.module}.{node.name}"
                node.__dict__["annotation_path"] = path
                node.__dict__["source_module"] = self.module
                self.names.setdefault(node.name, path)
                self.class_paths.add(path)
                for statement in node.body:
                    walk(statement, path)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, parent_path)

        walk(self.tree, None)

    def _attach_hints(
        self,
        node: ast.AST,
        class_path: str | None,
        class_node: ast.ClassDef | None,
        *,
        direct_class_body: bool,
    ) -> None:
        if isinstance(node, ast.ClassDef):
            path = node.__dict__.get("annotation_path")
            if isinstance(path, str):
                for statement in node.body:
                    self._attach_hints(statement, path, node, direct_class_body=True)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                node.__dict__["return_type"] = self._hint(node.returns)
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                self._attach_argument(argument)
            if node.args.vararg is not None:
                self._attach_argument(node.args.vararg)
            if node.args.kwarg is not None:
                self._attach_argument(node.args.kwarg)
            for child in ast.iter_child_nodes(node):
                self._attach_hints(child, class_path, class_node, direct_class_body=False)
            return
        if isinstance(node, ast.AnnAssign):
            hint = self._hint(node.annotation)
            node.__dict__["type_annotation"] = hint
            target = node.target
            if isinstance(target, ast.Name):
                target.__dict__["type_annotation"] = hint
                if direct_class_body and class_node is not None:
                    fields = class_node.__dict__.setdefault("field_types", {})
                    if isinstance(fields, dict):
                        fields[target.id] = hint
            elif (
                isinstance(target, ast.Attribute)
                and class_node is not None
                and isinstance(target.value, ast.Name)
                and target.value.id in {"self", "cls"}
            ):
                fields = class_node.__dict__.setdefault("field_types", {})
                if isinstance(fields, dict):
                    fields[target.attr] = hint
        for child in ast.iter_child_nodes(node):
            self._attach_hints(child, class_path, class_node, direct_class_body=False)

    def _attach_argument(self, argument: ast.arg) -> None:
        if argument.annotation is not None:
            argument.__dict__["type_annotation"] = self._hint(argument.annotation)

    def _hint(self, node: ast.expr) -> TypeHint:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                try:
                    parsed = ast.parse(node.value, mode="eval")
                except SyntaxError:
                    return TypeHint()
                return self._hint(parsed.body)
            return TypeHint()
        if isinstance(node, ast.Name):
            path = self.names.get(node.id)
            if path is not None:
                typing_name = self._typing_name(path)
                if typing_name == "Self":
                    return TypeHint(self_reference=True)
                if path.startswith(("typing.", "typing_extensions.")):
                    return TypeHint()
                return TypeHint((path,))
            return TypeHint()
        if isinstance(node, ast.Attribute):
            path = self._path(node)
            if path is None:
                return TypeHint()
            typing_name = self._typing_name(path)
            if typing_name == "Self":
                return TypeHint(self_reference=True)
            if path.startswith(("typing.", "typing_extensions.")):
                return TypeHint()
            return TypeHint((path,))
        if isinstance(node, ast.Subscript):
            path = self._path(node.value)
            typing_name = self._typing_name(path) if path is not None else None
            name = (
                typing_name
                or (path.rsplit(".", maxsplit=1)[-1] if path is not None else None)
                or (node.value.id if isinstance(node.value, ast.Name) else None)
            )
            if name == "Literal":
                return TypeHint()
            elements = self._slice_elements(node.slice)
            if name in {"Optional", "Union"}:
                return _merge_many([self._hint(element) for element in elements])
            if name in _WRAPPER_NAMES:
                return self._hint(elements[0]) if elements else TypeHint()
            arguments = tuple(self._hint(element) for element in elements)
            if path is not None and path in self.class_paths:
                # An internal generic class still produces that class as a value.
                return TypeHint((path,), arguments=arguments)
            if name in _TUPLE_NAMES:
                variadic = bool(
                    isinstance(node.slice, ast.Tuple)
                    and node.slice.elts
                    and isinstance(node.slice.elts[-1], ast.Constant)
                    and node.slice.elts[-1].value is Ellipsis
                )
                if variadic:
                    arguments = arguments[:-1]
                return TypeHint(origin="tuple", arguments=arguments, variadic=variadic)
            if name in _MAPPING_NAMES:
                if len(arguments) < 2:
                    arguments = (*arguments, TypeHint())
                return TypeHint(origin="mapping", arguments=arguments[:2])
            if name in _SEQUENCE_NAMES:
                return TypeHint(origin="sequence", arguments=arguments[:1])
            if name in _AWAITABLE_NAMES:
                return TypeHint(
                    origin="awaitable",
                    arguments=(arguments[-1],) if arguments else (),
                )
            return self._hint(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _merge(self._hint(node.left), self._hint(node.right))
        if isinstance(node, ast.IfExp):
            return _merge(self._hint(node.body), self._hint(node.orelse))
        return TypeHint()

    def _slice_elements(self, node: ast.expr) -> list[ast.expr]:
        return list(node.elts) if isinstance(node, ast.Tuple) else [node]

    def _path(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self._path(node.value)
            return f"{owner}.{node.attr}" if owner is not None else None
        return None

    @staticmethod
    def _typing_name(path: str) -> str | None:
        for prefix in ("typing.", "typing_extensions."):
            if path.startswith(prefix):
                return path.removeprefix(prefix).split(".", maxsplit=1)[0]
        return None

from __future__ import annotations

import ast
from typing import cast, override

__all__: list[str] = ["CollapseLiterals", "ReleaseCleanup", "strip_typing_imports"]


def strip_typing_imports(tree: ast.Module, typing_modules: set[str]) -> ast.Module:
    """Drop typing imports made unused by annotation/cast removal.

    Preserve postponed annotations: surviving fields may contain forward references.
    """
    used: set[str] = set()
    imports: list[ast.Import | ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
    for node in imports:
        if isinstance(node, ast.Import):
            if any(alias.name in typing_modules for alias in node.names):
                node.names = [
                    alias
                    for alias in node.names
                    if alias.name not in typing_modules or (alias.asname or alias.name) in used
                ]
        elif node.module in typing_modules:
            node.names = [
                name
                for name in node.names
                if (name.asname or name.name) in used or name.name == "*"
            ]

    class RemoveEmptyImports(ReleaseCleanup):
        @override
        def visit_Import(self, node: ast.Import) -> ast.AST | None:
            return node if node.names else None

        @override
        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
            return node if node.names else None

    return cast(ast.Module, RemoveEmptyImports(tree).visit(tree))


class ReleaseCleanup(ast.NodeTransformer):
    """Discard release-only metadata before python-minifier sees the source."""

    def __init__(
        self,
        tree: ast.Module,
        generic_classes: set[str] | None = None,
        *,
        module: str = "__main__",
        protocol_classes: set[tuple[str, str]] | None = None,
        discovery_only: bool = False,
    ) -> None:
        self.typing_modules = {"typing", "typing_extensions"}
        self.typing_names: dict[str, str] = {}
        self.dataclass_modules = {"dataclasses"}
        self.dataclass_names: set[str] = set()
        self.scope: ast.AST = tree
        self.field_names: set[str] = set()
        self.generic_classes = set(generic_classes or ())
        self.module = module
        self.is_package = getattr(tree, "is_package", False)
        self.protocol_classes = set(protocol_classes or ())
        classes: list[ast.ClassDef] = []
        from_imports: list[ast.ImportFrom] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(node)
                if node.type_params:
                    self.generic_classes.add(node.name)
            elif isinstance(node, ast.Import):
                for name in node.names:
                    if name.name in self.typing_modules:
                        self.typing_modules.add(name.asname or name.name)
                    elif name.name == "dataclasses":
                        self.dataclass_modules.add(name.asname or name.name)
            elif isinstance(node, ast.ImportFrom):
                from_imports.append(node)
                if node.module in self.typing_modules:
                    for name in node.names:
                        self.typing_names[name.asname or name.name] = name.name
                elif node.module == "dataclasses":
                    self.dataclass_names.update(
                        name.asname or name.name for name in node.names if name.name == "dataclass"
                    )
                    self.field_names.update(
                        name.asname or name.name for name in node.names if name.name == "field"
                    )
        self.typed_dicts: set[str] = set()
        self.protocol_names = {
            node.name
            for node in classes
            if any(self.typing_name(base) == "Protocol" for base in node.bases)
        }
        # Only named bases participate in inherited protocol discovery. Keep
        # module-local names distinct from imported names until an import edge
        # leads to an actual protocol class.
        self.protocol_edges: list[tuple[tuple[str, str], str]] = []
        if discovery_only:
            imports: dict[str, list[tuple[str, str]]] = {}
            for statement in from_imports:
                target = self.import_target(statement)
                for alias in statement.names:
                    if alias.name != "*":
                        imports.setdefault(alias.asname or alias.name, []).append((target, alias.name))
            for owner in classes:
                for base in owner.bases:
                    if isinstance(base, ast.Name):
                        self.protocol_edges.append(((self.module, base.id), owner.name))
                        self.protocol_edges.extend(
                            (key, owner.name) for key in imports.get(base.id, ())
                        )
            return
        self.protocol_classes.update((self.module, name) for name in self.protocol_names)
        imported_protocol_names = {
            alias.asname or alias.name
            for node in from_imports
            for alias in node.names
            if alias.name != "*" and (self.import_target(node), alias.name) in self.protocol_classes
        }
        while True:
            names = {
                node.name
                for node in classes
                if any(
                    isinstance(base, ast.Name)
                    and (base.id in self.protocol_names or base.id in imported_protocol_names)
                    for base in node.bases
                )
            }
            if names <= self.protocol_names:
                break
            self.protocol_names.update(names)
            self.protocol_classes.update((self.module, name) for name in names)
        while True:
            names = {
                node.name
                for node in classes
                if any(self.is_typed_dict(base) for base in node.bases)
            }
            if names <= self.typed_dicts:
                break
            self.typed_dicts.update(names)

    def import_target(self, node: ast.ImportFrom) -> str:
        package = self.module.split(".")[:-1]
        if self.is_package:
            package = self.module.split(".")
        if node.level:
            package = package[: len(package) - node.level + 1]
            return ".".join([*package, *([node.module] if node.module else [])])
        return node.module or ""

    def is_typed_dict(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Subscript):
            node = node.value
        return self.typing_name(node) == "TypedDict" or (
            isinstance(node, ast.Name) and node.id in self.typed_dicts
        )

    def typing_name(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.typing_names.get(node.id)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in self.typing_modules
        ):
            return node.attr
        return None

    def generic_visit(self, node: ast.AST) -> ast.AST:
        # Removing the only docstring/annotation must not leave an empty suite.
        suites = [
            field
            for field, value in ast.iter_fields(node)
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt)
        ]
        previous_scope = self.scope
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            self.scope = node
        node = super().generic_visit(node)
        self.scope = previous_scope
        for field in suites:
            if not getattr(node, field) and not isinstance(node, ast.Module):
                setattr(node, field, [ast.Pass()])
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
        target = self.import_target(node)
        node.names = [
            alias
            for alias in node.names
            if alias.name == "*" or (target, alias.name) not in self.protocol_classes
        ]
        return self.generic_visit(node) if node.names else None

    def visit_Assign(self, node: ast.Assign) -> ast.AST | None:
        node.targets = [
            target
            for target in node.targets
            if not isinstance(target, ast.Name) or target.id != "__all__"
        ]
        if not node.targets:
            return None
        node.type_comment = None
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if isinstance(node.target, ast.Name) and node.target.id == "__all__":
            return None
        if self.typing_name(node.annotation) == "TypeAlias":
            return self.visit_Assign(
                ast.copy_location(
                    ast.Assign(
                        targets=[node.target],
                        value=ast.Name(id="object", ctx=ast.Load()),
                    ),
                    node,
                )
            )
        if isinstance(self.scope, ast.ClassDef) and self.runtime_fields(self.scope):
            # Libraries such as dacite use the complete field type, not just
            # its presence. Only the preceding Literal collapse simplifies it;
            # do not apply the other typing or generic rewrites here.
            node.target = cast(ast.Name | ast.Attribute | ast.Subscript, self.visit(node.target))
            if node.value is not None:
                node.value = cast(ast.expr, self.visit(node.value))
            return node
        if node.value is not None:
            return self.visit_Assign(
                ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)
            )
        if isinstance(node.target, ast.Name):
            if isinstance(self.scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # An annotation-only local still makes the name local, even
                # when unassigned. Keep that compiler binding without a value.
                return ast.copy_location(
                    ast.If(
                        test=ast.Constant(False),
                        body=[ast.Assign(targets=[node.target], value=ast.Constant(None))],
                        orelse=[],
                    ),
                    node,
                )
            return None
        # Annotation-only attributes/subscripts evaluate the receiver/index,
        # but do not read or write the selected attribute/item.
        values = [node.target.value]
        if isinstance(node.target, ast.Subscript):

            def evaluated_index(index: ast.expr) -> ast.expr:
                if isinstance(index, ast.Slice):
                    return ast.Tuple(
                        elts=[value for value in (index.lower, index.upper, index.step) if value],
                        ctx=ast.Load(),
                    )
                if isinstance(index, ast.Tuple):
                    index.elts = [evaluated_index(value) for value in index.elts]
                return index

            values.append(evaluated_index(node.target.slice))
        return self.generic_visit(
            ast.copy_location(ast.Expr(value=ast.Tuple(elts=values, ctx=ast.Load())), node)
        )

    def runtime_fields(self, node: ast.ClassDef) -> bool:
        if any(self.typing_name(base) == "NamedTuple" for base in node.bases):
            return True
        for decorator in node.decorator_list:
            decorator = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(decorator, ast.Name) and decorator.id in self.dataclass_names:
                return True
            if (
                isinstance(decorator, ast.Attribute)
                and isinstance(decorator.value, ast.Name)
                and decorator.value.id in self.dataclass_modules
                and decorator.attr == "dataclass"
            ):
                return True
        return False

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST | None:
        if isinstance(node.target, ast.Name) and node.target.id == "__all__":
            return None
        return self.generic_visit(node)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> ast.AST:
        # Keep an inert binding: other modules may still import the alias.
        return ast.copy_location(
            ast.Assign(targets=[node.name], value=ast.Name(id="object", ctx=ast.Load())), node
        )

    def visit_If(self, node: ast.If) -> ast.AST | list[ast.stmt]:
        if self.typing_name(node.test) == "TYPE_CHECKING":
            wrapper = ast.Module(body=node.orelse, type_ignores=[])
            self.generic_visit(wrapper)
            return wrapper.body
        return self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.annotation = None
        node.type_comment = None
        return node

    def visit_arguments(self, node: ast.arguments) -> ast.AST:
        node.args = node.posonlyargs + node.args
        node.posonlyargs = []
        if node.vararg is None:
            # Required arguments must precede defaulted ones in Python syntax.
            defaults: list[ast.expr | None] = [None] * (len(node.args) - len(node.defaults))
            defaults.extend(node.defaults)
            parameters = list(zip(node.args, defaults, strict=True))
            parameters.extend(zip(node.kwonlyargs, node.kw_defaults, strict=True))
            node.args = [arg for arg, default in parameters if default is None]
            node.args.extend(arg for arg, default in parameters if default is not None)
            node.defaults = [default for _, default in parameters if default is not None]
            node.kwonlyargs = []
            node.kw_defaults = []
        # *args/**kwargs are real parameters, not removable separators.
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST | None:
        if any(self.typing_name(decorator) == "overload" for decorator in node.decorator_list):
            return None
        node.returns = None
        node.type_comment = None
        node.type_params = []
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST | None:
        return self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST | None:
        if node.name in self.protocol_names:
            return None
        if any(self.is_typed_dict(base) for base in node.bases):
            # TypedDict constructors already return plain dicts. Retain a binding
            # for runtime construction and cross-module imports, not the schema.
            return ast.copy_location(
                ast.Assign(
                    targets=[ast.Name(id=node.name, ctx=ast.Store())],
                    value=ast.Name(id="dict", ctx=ast.Load()),
                ),
                node,
            )
        type_parameters = {
            parameter.name
            for parameter in node.type_params
            if isinstance(parameter, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple))
        }
        node.type_params = []
        if type_parameters:

            class ReplaceTypeParameters(ast.NodeTransformer):
                def visit_Name(self, node: ast.Name) -> ast.AST:
                    if isinstance(node.ctx, ast.Load) and node.id in type_parameters:
                        return ast.copy_location(ast.Name(id="object", ctx=ast.Load()), node)
                    return node

            node = cast(ast.ClassDef, ReplaceTypeParameters().visit(node))
        return self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id in self.generic_classes:
            # Generic parameters are compile-time metadata in the checkout.
            # The release copy removes that metadata, so a runtime
            # ``Config[Document](...)`` must become ``Config(...)`` rather
            # than subscripting a non-generic class.
            return ast.copy_location(node.value, node.value)
        if self.typing_name(node.value) == "Literal":
            return ast.copy_location(ast.Name(id="object", ctx=ast.Load()), node)
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if self.typing_name(node.func) == "TypedDict":
            return ast.copy_location(ast.Name(id="dict", ctx=ast.Load()), node)
        if self.typing_name(node.func) == "cast" and len(node.args) == 2 and not node.keywords:
            return cast(ast.AST, self.visit(node.args[1]))
        if self.typing_name(node.func) in {"TypeVar", "TypeVarTuple", "ParamSpec"}:
            return ast.copy_location(ast.Name(id="object", ctx=ast.Load()), node)
        if (isinstance(node.func, ast.Name) and node.func.id in self.field_names) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.dataclass_modules
            and node.func.attr == "field"
        ):
            for argument in node.keywords:
                if argument.arg == "default_factory" and isinstance(argument.value, ast.Subscript):
                    argument.value = argument.value.value
        return self.generic_visit(node)


class CollapseLiterals(ast.NodeTransformer):
    """Widen literal values before cleanup preserves runtime field annotations."""

    def __init__(self, cleanup: ReleaseCleanup) -> None:
        self.cleanup = cleanup

    def literal_types(self, value: ast.expr) -> list[ast.expr] | None:
        if isinstance(value, ast.Constant):
            if value.value is None:
                return [ast.Constant(None)]
            return [ast.Name(id=type(value.value).__name__, ctx=ast.Load())]
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.UAdd, ast.USub)):
            return self.literal_types(value.operand)
        if isinstance(value, ast.Attribute):
            # Enum members are written as EnumClass.MEMBER (possibly qualified).
            return [value.value]
        if isinstance(value, ast.Subscript) and self.cleanup.typing_name(value.value) == "Literal":
            values: list[ast.expr] = (
                value.slice.elts if isinstance(value.slice, ast.Tuple) else [value.slice]
            )
            result: list[ast.expr] = []
            for item in values:
                widened: list[ast.expr] | None = self.literal_types(item)
                if widened is None:
                    return None
                result.extend(widened)
            return result
        # An imported/aliased value cannot be inferred from its spelling.
        return None

    def visit_Subscript(self, node: ast.Subscript) -> ast.expr:
        if self.cleanup.typing_name(node.value) != "Literal":
            return cast(ast.expr, self.generic_visit(node))
        widened: list[ast.expr] | None = self.literal_types(node)
        if not widened:
            return cast(ast.expr, self.generic_visit(node))
        types: dict[str, ast.expr] = {ast.dump(value): value for value in widened}
        result: ast.expr = next(iter(types.values()))
        for value in list(types.values())[1:]:
            result = ast.BinOp(left=result, op=ast.BitOr(), right=value)
        return ast.copy_location(result, node)

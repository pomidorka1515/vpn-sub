"""Fail-closed, build-only lowering of the repository's closed mixin families."""

from __future__ import annotations

import ast
from dataclasses import replace
from functools import cache
from typing import TYPE_CHECKING, Never, cast

from python_minifier.rename.binding import BuiltinBinding

from .analysis import inspects_frames
from .annotations import TypeHint, merge_hints
from .renamers.base import ArgumentRenamer

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__: list[str] = ["fold_mixins", "mark_mixin_origins"]

_ROOTS: dict[str, tuple[tuple[str, str], ...]] = {
    "api.admin": (("api.admin.routes", "AsyncApi"),),
    "api.web": (("api.web.routes", "AsyncWebApi"),),
    "db": (("db.database", "Database"),),
    "bots.telegram": (
        ("bots.telegram.admin.bot", "TelegramAdminBot"),
        ("bots.telegram.public.bot", "TelegramPublicBot"),
    ),
    "bots.discord": (
        ("bots.discord.admin.bot", "DiscordAdminBot"),
        ("bots.discord.public.bot", "DiscordPublicBot"),
    ),
}
_API_BASE: tuple[str, str] = ("api.common", "AsyncBaseApi")
_API_DECORATORS: frozenset[tuple[str, str]] = frozenset(
    {
        ("api.decorators.auth", "requires_admin_auth"),
        ("api.decorators.auth", "requires_webapi_auth"),
        ("api.decorators.auth", "requires_no_auth"),
        ("api.decorators.validation", "requires_fields"),
        ("api.decorators.validation", "requires_fields_strict"),
        ("api.decorators.validation", "requires_args"),
    }
)
_HOOKS: frozenset[str] = frozenset(
    {
        "__slots__",
        "__new__",
        "__init_subclass__",
        "__class_getitem__",
        "__mro_entries__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__dict__",
        "__weakref__",
        "__classcell__",
        "__class__",
        "__doc__",
        "__firstlineno__",
        "__static_attributes__",
        "__annotate__",
        "__type_params__",
        "__annotations__",
        "__module__",
        "__qualname__",
    }
)


def mark_mixin_origins(tree: ast.Module, module: str) -> None:
    """Keep provenance and erased architecture syntax through cleanup/linking/renaming."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.__dict__["mixin_origin"] = (module, node.name)
        if isinstance(node, ast.ClassDef):
            node.__dict__["mixin_origin"] = (module, node.name)
            node.__dict__["mixin_raw_architecture"] = bool(
                node.decorator_list
                or node.keywords
                or node.type_params
                or any(not isinstance(base, (ast.Name, ast.Attribute)) for base in node.bases)
            )
            node.__dict__["mixin_base_count"] = len(node.bases)


def _origin(node: ast.ClassDef) -> tuple[str, str]:
    return cast(tuple[str, str], node.__dict__.get("mixin_origin", ("", node.name)))


def _fail(node: ast.ClassDef, reason: str) -> Never:
    module, name = _origin(node)
    raise ValueError(f"Mixin folding: {module}.{name}: {reason}; update the architecture contract")


def _private(name: str) -> bool:
    return name.startswith("__") and not name.endswith("__")


def _api_module(module: str) -> bool:
    return module in {"api.admin", "api.web"} or module.startswith(("api.admin.", "api.web."))


def _literal(node: ast.expr) -> bool:
    """Class data must not execute code or depend on the old class namespace."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal(value) for value in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _literal(key) for key in node.keys) and all(
            _literal(value) for value in node.values
        )
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float, complex))
    )


class _ApiArchitecture:
    """Recognize API definition-time data and wrappers without executing them."""

    def __init__(self, analysis: ArgumentRenamer, family: set[ast.ClassDef]) -> None:
        self.analysis: ArgumentRenamer = analysis
        self.family: set[ast.ClassDef] = family

    def origin(self, node: ast.expr) -> tuple[str, str] | None:
        target = self.analysis.target(node, instances=False)
        if target is not None:
            origin = target.__dict__.get("mixin_origin")
            if isinstance(origin, tuple):
                return cast(tuple[str, str], origin)
        path = self.analysis.imported_path(node)
        if path is not None and "." in path:
            module, name = path.rsplit(".", 1)
            return module, name
        return None

    def builtin(self, node: ast.expr, names: set[str]) -> bool:
        return (
            isinstance(node, ast.Name)
            and node.id in names
            and isinstance(cast(object, self.analysis.bindings.get(node)), BuiltinBinding)
        )

    def route_reference(self, node: ast.expr, seen: frozenset[ast.ClassDef]) -> bool:
        if not isinstance(node, ast.Attribute) or node.attr != "ROUTES":
            return False
        owner = self.analysis.target(node.value, instances=False)
        if not isinstance(owner, ast.ClassDef) or owner not in self.family or owner in seen:
            return False
        definitions = [
            statement.value
            for statement in owner.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "ROUTES"
        ]
        return len(definitions) == 1 and self.routes(definitions[0], seen | {owner})

    def routes(self, node: ast.expr, seen: frozenset[ast.ClassDef] = frozenset()) -> bool:
        if not isinstance(node, ast.Tuple):
            return False
        for value in node.elts:
            if isinstance(value, ast.Starred) and self.route_reference(value.value, seen):
                continue
            if not (
                isinstance(value, ast.Call)
                and self.origin(value.func) == ("api.common", "Route")
                and all(_literal(argument) for argument in value.args)
                and all(
                    keyword.arg is not None and _literal(keyword.value)
                    for keyword in value.keywords
                )
            ):
                return False
        return True

    def endpoints(self, node: ast.expr) -> bool:
        if not (
            isinstance(node, ast.Call)
            and self.builtin(node.func, {"frozenset"})
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Set)
        ):
            return False
        for value in node.args[0].elts:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                continue
            if not (isinstance(value, ast.Starred) and isinstance(value.value, ast.GeneratorExp)):
                return False
            generator = value.value
            if len(generator.generators) != 1:
                return False
            loop = generator.generators[0]
            if not (
                isinstance(loop.target, ast.Name)
                and not loop.ifs
                and not loop.is_async
                and self.route_reference(loop.iter, frozenset())
                and isinstance(generator.elt, ast.Attribute)
                and generator.elt.attr == "handler"
                and isinstance(generator.elt.value, ast.Name)
                and generator.elt.value.id == loop.target.id
            ):
                return False
        return True

    def data(self, name: str, value: ast.expr) -> bool:
        return (name == "ROUTES" and self.routes(value)) or (
            name == "_NO_STORE_ENDPOINTS" and self.endpoints(value)
        )

    def decorator(self, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return self.origin(node) in {
                origin for origin in _API_DECORATORS if origin[0] == "api.decorators.auth"
            }
        return (
            self.origin(node.func)
            in {origin for origin in _API_DECORATORS if origin[0] == "api.decorators.validation"}
            and not node.keywords
            and all(
                _literal(child)
                or self.builtin(child, {"str", "int", "bool", "float", "list", "dict"})
                for argument in node.args
                for child in (argument.elts if isinstance(argument, ast.Tuple) else [argument])
            )
        )

    def decorators(self, nodes: list[ast.expr]) -> bool:
        return (len(nodes) == 1 and self.builtin(nodes[0], {"property"})) or (
            bool(nodes) and all(self.decorator(node) for node in nodes)
        )


def fold_mixins(tree: ast.Module, modules: Iterable[str]) -> int:
    """Fold closed feature hierarchies into their concrete compositions.

    Run BEFORE method/parameter renaming and allowlisting so safety analysis sees
    concrete methods and explicit, statically resolvable dispatch.
    Default values, decorator references, and data are evaluated at their original
    positions. Methods are real class-body definitions; separate consumers get
    separate functions, but retain shared defaults/data. No mixin is constructed.
    """
    module_names = set(modules)
    complete_application = {"main", "app", "asgi"} <= module_names
    expected = {
        origin
        for package, roots in _ROOTS.items()
        if complete_application
        or any(module == package or module.startswith(package + ".") for module in module_names)
        for origin in roots
    }
    if not expected:
        return 0
    if inspects_frames(tree):
        raise ValueError(
            "Mixin folding: frame inspection is incompatible with the closed architecture"
        )
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    origins = {_origin(node): node for node in classes}
    for origin in sorted(expected):
        if origin not in origins:
            raise ValueError(f"Mixin folding: missing composition root {'.'.join(origin)}")
    roots = [origins[origin] for origin in sorted(expected)]
    names: dict[str, ast.ClassDef] = {}
    for node in classes:
        alias = node.__dict__.get("class_alias_target")
        name = alias.id if isinstance(alias, ast.Name) else node.name
        if name in names:
            _fail(node, "ambiguous linked class binding")
        names[name] = node

    api_base = origins.get(_API_BASE)

    orders: dict[ast.ClassDef, list[ast.ClassDef]] = {}
    visiting: set[ast.ClassDef] = set()

    def mro(node: ast.ClassDef, package: str) -> list[ast.ClassDef]:
        module, name = _origin(node)
        if package in {"api.admin", "api.web"} and node is api_base:
            # Retain the shared ABC and its import-time route validation hook.
            return [node]
        if not (module == package or module.startswith(package + ".")):
            _fail(node, "base outside its closed package")
        if node not in roots and not (
            name.endswith("Mixin")
            or (package in {"api.admin", "api.web"} and name.endswith("Routes"))
        ):
            _fail(node, "base is not a feature mixin")
        if node in visiting:
            _fail(node, "inheritance cycle")
        if node in orders:
            return orders[node]
        if (
            node.__dict__.get("mixin_raw_architecture")
            or node.decorator_list
            or node.keywords
            or node.type_params
            or node.__dict__.get("mixin_base_count", len(node.bases)) != len(node.bases)
        ):
            _fail(node, "decorated, generic, metaclass, or rewritten base architecture")
        direct: list[ast.ClassDef] = []
        for base in node.bases:
            if not isinstance(base, ast.Name) or base.id not in names:
                _fail(node, "unknown or external base")
            ancestor = names[base.id]
            if ancestor in roots:
                _fail(node, "composition roots cannot inherit from one another")
            if ancestor is api_base and node not in roots:
                _fail(node, "only API composition roots may inherit AsyncBaseApi")
            direct.append(ancestor)
        if len(set(direct)) != len(direct):
            _fail(node, "duplicate base")
        visiting.add(node)
        sequences = [mro(base, package).copy() for base in direct] + [direct.copy()]
        order = [node]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if all(sequence[0] not in other[1:] for other in sequences)
                ),
                None,
            )
            if candidate is None:
                _fail(node, "inconsistent C3 method order")
            order.append(candidate)
            for sequence in sequences:
                if sequence[0] is candidate:
                    sequence.pop(0)
        visiting.remove(node)
        orders[node] = order
        return order

    for root in roots:
        package = next(package for package, values in _ROOTS.items() if _origin(root) in values)
        if not root.bases:
            _fail(root, "composition root no longer has mixins")
        mro(root, package)
        if package in {"api.admin", "api.web"} and (
            api_base is None
            or orders[root][-1] is not api_base
            or not any(
                isinstance(base, ast.Name) and names.get(base.id) is api_base for base in root.bases
            )
        ):
            _fail(root, "API root must directly retain AsyncBaseApi last in its C3 order")
    family = set(orders)
    mixins = family - set(roots)
    # A newly added but uncomposed mixin, or a new concrete composition, must not
    # silently remain in the runtime output.
    for node in classes:
        module, name = _origin(node)
        in_package = any(
            module == package or module.startswith(package + ".")
            for package in _ROOTS
            if any(origin in expected for origin in _ROOTS[package])
        )
        api_group = _api_module(module) and name.endswith("Routes")
        if in_package and (name.endswith("Mixin") or api_group) and node not in family:
            _fail(node, "unconnected mixin")

    analysis = ArgumentRenamer({"__main__": tree})
    api = _ApiArchitecture(analysis, family)
    if api_base is not None and any(api_base in orders[root] for root in roots):
        if (
            api_base.__dict__.get("mixin_raw_architecture")
            or len(api_base.bases) != 1
            or analysis.imported_path(api_base.bases[0]) != "abc.ABC"
            or api_base.__dict__.get("mixin_base_count", 1) != 1
            or any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name in _HOOKS - {"__init_subclass__"}
                for member in api_base.body
            )
        ):
            _fail(api_base, "unsupported shared API base architecture")
        for root in roots:
            if orders[root] == [root, api_base]:
                _fail(root, "composition root no longer has mixins")
    for child in analysis.nodes:
        if (
            isinstance(child, ast.Name)
            and child.id in {"globals", "locals"}
            and isinstance(cast(object, analysis.bindings.get(child)), BuiltinBinding)
        ):
            raise ValueError(
                "Mixin folding: namespace inspection is incompatible with the closed architecture"
            )
    alias_nodes: set[ast.AST] = set()
    removed_statements: set[ast.stmt] = set()
    for node in mixins:
        alias = node.__dict__.get("class_alias_target")
        references = cast(list[ast.Name], node.__dict__.get("class_alias_references", []))
        if isinstance(alias, ast.Name):
            alias_nodes.update([alias, *references])
            removed_statements.update(
                cast(ast.stmt, analysis.parents[reference]) for reference in [alias, *references]
            )
    base_nodes = {base for node in family for base in node.bases}
    mixin_names = {name for name, node in names.items() if node in mixins}
    for child in analysis.nodes:
        if isinstance(child, ast.ClassDef) and child not in family:
            for base in child.bases:
                for reference in ast.walk(base):
                    if not isinstance(reference, (ast.Name, ast.Attribute)):
                        continue
                    hierarchy_target = analysis.target(reference, instances=False)
                    if (
                        hierarchy_target is api_base
                        and api_base is not None
                        and child not in roots
                        and any(api_base in orders[root] for root in roots)
                    ):
                        _fail(child, "shared API base has a consumer outside the composition roots")
                    if hierarchy_target in mixins:
                        _fail(child, "mixin has a consumer outside the composition roots")
                    if hierarchy_target in roots:
                        _fail(child, "composition root has a subclass")
        if (
            isinstance(child, ast.Attribute)
            and child.attr
            in {
                "__mro__",
                "__bases__",
                "__subclasses__",
                "__dict__",
                "__annotations__",
            }
            and analysis.target(child.value) in family
        ):
            _fail(
                cast(ast.ClassDef, analysis.target(child.value)),
                "hierarchy or namespace reflection",
            )

    used = {node.id for node in analysis.nodes if isinstance(node, ast.Name)}
    used.update(
        node.name
        for node in analysis.nodes
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    used.update(node.arg for node in analysis.nodes if isinstance(node, ast.arg))
    used.update(node.attr for node in analysis.nodes if isinstance(node, ast.Attribute))
    number = 0

    def fresh() -> str:
        nonlocal number
        while True:
            name = f"_m{number}"
            number += 1
            if name not in used:
                used.add(name)
                return name

    members: dict[ast.ClassDef, dict[str, tuple[str, ast.stmt]]] = {}
    descriptors: dict[ast.stmt, str | None] = {}
    for owner in classes:
        if owner not in family:
            continue
        selected: dict[str, tuple[str, ast.stmt]] = {}
        is_api = _api_module(_origin(owner)[0])
        members[owner] = selected
        class_names = {
            child.name
            for child in owner.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        class_names.update(
            target.id
            for child in owner.body
            if isinstance(child, ast.Assign)
            for target in child.targets
            if isinstance(target, ast.Name)
        )
        for statement in owner.body:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = statement.name
                descriptor = analysis.descriptor(statement)
                descriptors[statement] = descriptor
                if statement.decorator_list and not (
                    descriptor
                    or analysis.forwarding_decorators(statement)
                    or (is_api and api.decorators(statement.decorator_list))
                ):
                    _fail(owner, f"unsupported method decorator on {name}")
                headers = [
                    *statement.decorator_list,
                    *statement.args.defaults,
                    *(value for value in statement.args.kw_defaults if value is not None),
                ]
                for header in headers:
                    if any(
                        child.id in class_names
                        for child in ast.walk(header)
                        if isinstance(child, ast.Name)
                    ):
                        _fail(owner, f"class-local method header on {name}")
                for child in ast.walk(statement):
                    spelling = (
                        child.id
                        if isinstance(child, ast.Name)
                        else child.attr
                        if isinstance(child, ast.Attribute)
                        else child.arg
                        if isinstance(child, (ast.arg, ast.keyword))
                        else None
                    )
                    if spelling == "__class__" or (spelling is not None and _private(spelling)):
                        _fail(owner, f"class cell or private name mangling in {name}")
                    if isinstance(child, ast.Name) and child.id in {
                        "locals",
                        "globals",
                        "vars",
                        "dir",
                    }:
                        _fail(owner, f"namespace reflection in {name}")
            elif (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and (
                    _literal(statement.value)
                    or (is_api and api.data(statement.targets[0].id, statement.value))
                )
            ):
                name = statement.targets[0].id
            else:
                _fail(owner, "unsupported executable class body")
            if name in selected or name in _HOOKS or _private(name):
                _fail(owner, f"duplicate, private, or architecture-sensitive member {name}")
            selected[name] = (fresh(), statement)

    data_references: dict[ast.Attribute, str] = {}
    for child in analysis.nodes:
        if not isinstance(child, ast.Name) or child.id not in mixin_names:
            continue
        if child in base_nodes or child in alias_nodes:
            continue
        # Ignore a same-spelled, genuinely local variable, not global uses.
        binding = analysis.bindings.get(child)
        if binding is None or binding not in analysis.node_bindings(tree):
            continue
        owner = names[child.id]
        parent = analysis.parents.get(child)
        if isinstance(parent, ast.Attribute) and isinstance(parent.ctx, ast.Load):
            target = next(
                (
                    members[base][parent.attr]
                    for base in orders[owner]
                    if parent.attr in members[base]
                ),
                None,
            )
            if target is not None:
                slot, definition = target
                # The database backup validator reads SchemaMixin.SCHEMA_VERSION.
                # Permit direct reads of immutable data, never class identity,
                # method extraction, mutable data, reflection, or mutation.
                if isinstance(definition, ast.Assign) and (
                    (api.data(parent.attr, definition.value) and _api_module(_origin(owner)[0]))
                    or (
                        _literal(definition.value)
                        and all(
                            not isinstance(value, (ast.List, ast.Dict, ast.Set))
                            for value in ast.walk(definition.value)
                        )
                    )
                ):
                    data_references[parent] = slot
                    continue
        _fail(owner, "mixin used as a runtime value or mutated outside its body")

    # Resolve every supported super call against every consumer's ORIGINAL C3
    # order. A shared method whose next implementation differs is not lowerable.
    replacements: dict[ast.Call, tuple[str, str]] = {}
    for owner, own_members in members.items():
        for _, statement in own_members.values():
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(statement):
                if not isinstance(child, ast.Name) or child.id != "super":
                    continue
                call = analysis.parents.get(child)
                attribute = analysis.parents.get(call) if call is not None else None
                invocation = analysis.parents.get(attribute) if attribute is not None else None
                scope = analysis.parents.get(child)
                while scope is not None and not isinstance(
                    scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
                ):
                    scope = analysis.parents.get(scope)
                receiver = [*statement.args.posonlyargs, *statement.args.args]
                if not (
                    isinstance(call, ast.Call)
                    and call.func is child
                    and not call.args
                    and not call.keywords
                    and isinstance(attribute, ast.Attribute)
                    and isinstance(invocation, ast.Call)
                    and invocation.func is attribute
                    and scope is statement
                    and receiver
                    and descriptors[statement] is None
                    and not statement.decorator_list
                    and isinstance(cast(object, analysis.bindings.get(child)), BuiltinBinding)
                ):
                    _fail(owner, "unsupported super expression")
                targets: set[str] = set()
                for root in roots:
                    order = orders[root]
                    if owner not in order:
                        continue
                    target = next(
                        (
                            members[base][attribute.attr]
                            for base in order[order.index(owner) + 1 :]
                            if base in members and attribute.attr in members[base]
                        ),
                        None,
                    )
                    if target is None:
                        if (
                            api_base is not None
                            and order[-1] is api_base
                            and any(
                                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                                and member.name == attribute.attr
                                for member in api_base.body
                            )
                        ):
                            # The copied method's new class cell dispatches to
                            # the same retained base after all features are folded.
                            continue
                        _fail(owner, f"super target {attribute.attr} is not an internal method")
                    slot, definition = target
                    if (
                        not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                        or descriptors[definition] is not None
                        or definition.decorator_list
                    ):
                        _fail(owner, "super target is not an ordinary instance method")
                    targets.add(slot)
                if not targets:
                    continue
                if len(targets) != 1:
                    _fail(owner, "shared super method has consumer-dependent dispatch")
                replacements[invocation] = (targets.pop(), receiver[0].arg)

    class RewriteData(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            slot = data_references.get(node)
            if slot is not None:
                return ast.copy_location(ast.Name(id=slot, ctx=ast.Load()), node)
            return self.generic_visit(node)

    RewriteData().visit(tree)
    # Capture values, not functions, at the old definition sites. In particular,
    # mutable defaults must not be evaluated once per concrete consumer.
    captured_headers: dict[ast.stmt, list[ast.stmt]] = {}
    for own_members in members.values():
        for _, statement in own_members.values():
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            captured: list[ast.stmt] = []

            def capture(value: ast.expr, captured: list[ast.stmt] = captured) -> ast.Name:
                slot = fresh()
                captured.append(
                    ast.Assign(targets=[ast.Name(id=slot, ctx=ast.Store())], value=value)
                )
                return ast.Name(id=slot, ctx=ast.Load())

            # Python evaluates decorator expressions before default expressions.
            statement.decorator_list = [capture(value) for value in statement.decorator_list]
            statement.args.defaults = [capture(value) for value in statement.args.defaults]
            statement.args.kw_defaults = [
                capture(value) if value is not None else None
                for value in statement.args.kw_defaults
            ]
            captured_headers[statement] = captured

    class RewriteSuper(ast.NodeTransformer):
        def __init__(self, selected_names: dict[str, str]) -> None:
            self.selected_names: dict[str, str] = selected_names

        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            replacement = replacements.get(node)
            if replacement is not None:
                slot, receiver = replacement
                node.func = ast.Attribute(
                    value=ast.Name(id=receiver, ctx=ast.Load()),
                    attr=self.selected_names.get(slot, slot),
                    ctx=ast.Load(),
                )
            return node

    body: list[ast.stmt] = []
    next_line = (
        max((getattr(child, "end_lineno", 0) or 0 for child in ast.walk(tree)), default=0) + 1
    )
    for top_level in tree.body:
        if top_level in removed_statements:
            continue
        if not isinstance(top_level, ast.ClassDef) or top_level not in family:
            body.append(top_level)
            continue
        node = top_level
        for slot, statement in members[node].values():
            if isinstance(statement, ast.Assign):
                body.append(
                    ast.Assign(targets=[ast.Name(id=slot, ctx=ast.Store())], value=statement.value)
                )
                continue
            body.extend(captured_headers[statement])
        if node in mixins:
            continue
        effective: dict[str, tuple[str, ast.stmt]] = {}
        for ancestor in orders[node]:
            if ancestor is api_base:
                continue
            for name, member in members[ancestor].items():
                effective.setdefault(name, member)
        # Cleanup erased the declarations, but later rename safety still needs
        # the inherited receiver types after their declaring classes disappear.
        field_types: dict[str, TypeHint] = {}
        for ancestor in orders[node]:
            inherited = ancestor.__dict__.get("field_types", {})
            for name, hint in cast(dict[str, object], inherited).items():
                if isinstance(hint, TypeHint):
                    field_types[name] = merge_hints(field_types.get(name, TypeHint()), hint)
        node.__dict__["field_types"] = field_types
        selected_names = {slot: name for name, (slot, _) in effective.items()}
        definitions = dict(effective)
        # Retain only overridden implementations reachable through super calls.
        pending = list(definitions.values())
        while pending:
            _, definition = pending.pop()
            for child in ast.walk(definition):
                if not isinstance(child, ast.Call) or child not in replacements:
                    continue
                target_slot, _ = replacements[child]
                if target_slot in selected_names:
                    continue
                member = next(
                    member
                    for ancestor in orders[node]
                    for member in members[ancestor].values()
                    if member[0] == target_slot
                )
                selected_names[target_slot] = target_slot
                definitions[target_slot] = member
                pending.append(member)
        node.bases = [
            base
            for base in node.bases
            if isinstance(base, ast.Name) and names.get(base.id) is api_base
        ]
        node.body = []
        for name, (slot, definition) in definitions.items():
            if isinstance(definition, ast.Assign):
                node.body.append(
                    ast.Assign(
                        targets=[ast.Name(id=name, ctx=ast.Store())],
                        value=ast.Name(id=slot, ctx=ast.Load()),
                    )
                )
                continue
            if (
                isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                and definition.__dict__.get("no_rename")
                and name != definition.name
            ):
                _fail(node, f"no_rename prevents lowering overridden method {definition.name}")
            # Rewrite a temporary parse, not the shared source AST: each concrete
            # class may use different member names for the same implementation.
            calls = [child for child in ast.walk(definition) if isinstance(child, ast.Call)]
            rewritten = RewriteSuper(selected_names)
            # Rewrite in place briefly so identity-keyed super resolutions apply,
            # then parse a clean copy without stale linker/renamer metadata.
            originals = [(call, call.func) for call in calls if call in replacements]
            rewritten.visit(definition)
            method = cast(
                ast.FunctionDef | ast.AsyncFunctionDef,
                ast.parse(ast.unparse(ast.fix_missing_locations(definition))).body[0],
            )
            for original_node, copied_node in zip(
                ast.walk(definition), ast.walk(method), strict=True
            ):
                for key in (
                    "original_name",
                    "original_file",
                    "type_annotation",
                    "return_type",
                    "expression_type",
                    "type_parameters",
                    "annotation_path",
                    "source_module",
                    "no_rename",
                ):
                    if key in original_node.__dict__:
                        copied_node.__dict__[key] = original_node.__dict__[key]
            for call, func in originals:
                call.func = func
            method.name = name
            method.__dict__["original_name"] = name
            # Distinct positions prevent CPython coalescing identical code
            # constants across consumers and retaining the first qualname.
            ast.increment_lineno(method, next_line)
            next_line = (method.end_lineno or next_line) + 1
            node.body.append(method)
        if not node.body:
            node.body = [ast.Pass()]
        body.append(node)
    tree.body = body
    hint_paths = {
        path: tuple(
            cast(str, root.__dict__["annotation_path"])
            for root in roots
            if mixin in orders[root] and isinstance(root.__dict__.get("annotation_path"), str)
        )
        for mixin in mixins
        if isinstance(path := mixin.__dict__.get("annotation_path"), str)
    }

    @cache
    def rewrite_hint(hint: TypeHint) -> TypeHint:
        return replace(
            hint,
            classes=tuple(
                dict.fromkeys(
                    target for path in hint.classes for target in hint_paths.get(path, (path,))
                )
            ),
            arguments=tuple(rewrite_hint(argument) for argument in hint.arguments),
        )

    # Helpers and nested container hints can still name erased feature classes.
    # Represent those types by every concrete consumer, never an arbitrary root.
    for child in ast.walk(tree):
        for key in ("type_annotation", "return_type", "expression_type"):
            hint = child.__dict__.get(key)
            if isinstance(hint, TypeHint):
                child.__dict__[key] = rewrite_hint(hint)
        fields = child.__dict__.get("field_types")
        if isinstance(fields, dict):
            child.__dict__["field_types"] = {
                name: rewrite_hint(hint) if isinstance(hint, TypeHint) else hint
                for name, hint in cast(dict[str, object], fields).items()
            }
    ast.fix_missing_locations(tree)
    return len(mixins)

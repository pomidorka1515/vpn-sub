"""Fail-closed, build-only lowering of the repository's closed mixin families."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Never, cast

from python_minifier.rename.binding import BuiltinBinding

from .analysis import inspects_frames
from .renamers.base import ArgumentRenamer

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__: list[str] = ["fold_mixins", "mark_mixin_origins"]

_ROOTS: dict[str, tuple[tuple[str, str], ...]] = {
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


def fold_mixins(tree: ast.Module, modules: Iterable[str]) -> int:
    """Replace the five concrete compositions with ordinary object-based classes.

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

    orders: dict[ast.ClassDef, list[ast.ClassDef]] = {}
    visiting: set[ast.ClassDef] = set()

    def mro(node: ast.ClassDef, package: str) -> list[ast.ClassDef]:
        module, name = _origin(node)
        if not (module == package or module.startswith(package + ".")):
            _fail(node, "base outside its closed package")
        if node not in roots and not name.endswith("Mixin"):
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
        if in_package and name.endswith("Mixin") and node not in family:
            _fail(node, "unconnected mixin")

    analysis = ArgumentRenamer({"__main__": tree})
    for child in ast.walk(tree):
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
    for child in ast.walk(tree):
        if isinstance(child, ast.ClassDef) and child not in family:
            for base in child.bases:
                for reference in ast.walk(base):
                    if not isinstance(reference, (ast.Name, ast.Attribute)):
                        continue
                    hierarchy_target = analysis.target(reference, instances=False)
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

    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    used.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    used.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
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
                    descriptor or analysis.forwarding_decorators(statement)
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
                and _literal(statement.value)
            ):
                name = statement.targets[0].id
            else:
                _fail(owner, "unsupported executable class body")
            if name in selected or name in _HOOKS or _private(name):
                _fail(owner, f"duplicate, private, or architecture-sensitive member {name}")
            selected[name] = (fresh(), statement)

    data_references: dict[ast.Attribute, str] = {}
    for child in ast.walk(tree):
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
                if isinstance(definition, ast.Assign) and all(
                    not isinstance(value, (ast.List, ast.Dict, ast.Set))
                    for value in ast.walk(definition.value)
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
                            if attribute.attr in members[base]
                        ),
                        None,
                    )
                    if target is None:
                        _fail(owner, f"super target {attribute.attr} is not an internal method")
                    slot, definition = target
                    if (
                        not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                        or descriptors[definition] is not None
                        or definition.decorator_list
                    ):
                        _fail(owner, "super target is not an ordinary instance method")
                    targets.add(slot)
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
            for name, member in members[ancestor].items():
                effective.setdefault(name, member)
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
        node.bases = []
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
                    "annotation_path",
                    "source_module",
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
    ast.fix_missing_locations(tree)
    return len(mixins)

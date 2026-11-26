from __future__ import annotations

import ast
from typing import cast

from python_minifier.rename.binding import BuiltinBinding

from .analysis import inspects_frames
from .renamers.allowlist import AllowlistRenamer
from .renamers.external import ExternalReturns

__all__: list[str] = ["eliminate_dead_code"]

type Definition = ast.FunctionDef | ast.AsyncFunctionDef
type DeadSymbol = tuple[str, tuple[str, ...], str | None]


def _literal(node: ast.expr) -> bool:
    """Only discard defaults whose evaluation cannot execute application code."""
    return isinstance(node, ast.Constant) or (
        isinstance(node, ast.Tuple) and all(_literal(item) for item in node.elts)
    )


def eliminate_dead_code(tree: ast.Module) -> None:
    """Remove unreachable, inert function definitions from a linked program.

    Uncertain receiver uses, opaque inheritance and reflection retain code.
    Definition headers execute in the enclosing scope, independently of calls.
    """
    if inspects_frames(tree) or tree.__dict__.get("tainted"):
        return
    analysis = AllowlistRenamer(tree)
    _, _, reflected = analysis.analyze_calls()
    functions = {
        node
        for node in analysis.functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    used_members = {
        node.attr for node in analysis.nodes if isinstance(node, ast.Attribute)
    } | analysis.strings
    closed_families: dict[ast.ClassDef, bool] = {}
    escape_nodes = [
        node for node in analysis.nodes
        if isinstance(node, ast.Call)
        or (
            isinstance(node, ast.Attribute)
            and node.attr in {"__dict__", "__getattribute__", "__getattr__"}
        )
    ]
    receiver_types: dict[ast.AST, set[ast.ClassDef]] = {}
    # External annotations depend only on lexical analysis. Reading one must
    # not solve the unrelated whole-program attribute/callback flow graph.
    external_returns = ExternalReturns(analysis)

    def types(node: ast.AST) -> set[ast.ClassDef]:
        if node not in receiver_types:
            receiver_types[node] = analysis.type_targets(node)
        return receiver_types[node]

    def unused_inherited_method(function: Definition) -> bool:
        owner = analysis.parents.get(function)
        if (
            not isinstance(owner, ast.ClassDef)
            or not owner.bases
            or function.name in used_members
            or tree.__dict__.get("tainted")
        ):
            return False
        try:
            family = analysis.receiver_family(owner)
            for receiver in family:
                analysis.check_receiver(receiver, function.name)
        except ImportError, AttributeError, OSError, TypeError, ValueError, SyntaxError:
            return False
        if owner not in closed_families:
            closed_families[owner] = closed_family(family)
        return closed_families[owner]

    def closed_family(family: frozenset[ast.ClassDef]) -> bool:
        # An arbitrary consumer can use APIs beyond the inherited contract.
        # Direct external calls receiving this class/instance remain opaque.
        for node in escape_nodes:
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"__dict__", "__getattribute__", "__getattr__"}
                and types(node.value) & family
            ):
                return False
            if not isinstance(node, ast.Call):
                continue
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if not any(types(argument) & family for argument in arguments):
                continue
            target = analysis.target(node.func)
            if isinstance(
                target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            if isinstance(node.func, ast.Name):
                # Builtin protocol operations dispatch special methods, not an
                # arbitrary newly introduced method on an external subclass.
                if not isinstance(
                    cast(object, analysis.bindings.get(node.func)), BuiltinBinding
                ) or node.func.id not in {
                    "isinstance",
                    "issubclass",
                    "type",
                    "len",
                    "bool",
                    "hash",
                    "repr",
                    "str",
                    "iter",
                    "next",
                    "super",
                }:
                    return False
            elif isinstance(node.func, ast.Attribute):
                hint = external_returns.hint(node.func.value)
                containers = {
                    "weakref.WeakSet": {"add", "discard", "remove"},
                    "_weakrefset.WeakSet": {"add", "discard", "remove"},
                    "builtins.set": {"add", "discard", "remove"},
                    "builtins.list": {"append"},
                }
                if (
                    not hint.classes
                    or hint.incomplete
                    or any(
                        node.func.attr not in containers.get(name, set()) for name in hint.classes
                    )
                ):
                    return False
        return True

    candidates: set[Definition] = set()
    for function in functions:
        binding = analysis.bindings.get(function)
        if (
            (
                not function.decorator_list
                or analysis.descriptor(function) is not None
                or analysis.forwarding_decorators(function)
            )
            and not function.name.startswith("__")
            and not function.type_params
            and all(_literal(value) for value in function.args.defaults)
            and all(value is None or _literal(value) for value in function.args.kw_defaults)
            and binding is not None
            and analysis.definitions(binding) == [function]
            and not any(isinstance(ref, ast.Nonlocal) for ref in binding.references)
            and (function not in reflected or unused_inherited_method(function))
        ):
            candidates.add(function)

    def enclosing(node: ast.AST) -> Definition | None:
        while node in analysis.parents:
            parent = analysis.parents[node]
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node in parent.body:
                    return parent
                # Defaults/decorators execute when the definition is reached.
            node = parent
        return None

    edges: dict[Definition | None, set[Definition]] = {None: functions - candidates}
    for node in analysis.nodes:
        targets: set[Definition] = set()
        if isinstance(node, ast.Name):
            binding = analysis.bindings.get(node)
            if binding is not None:
                targets.update(
                    definition
                    for definition in analysis.definitions(binding)
                    if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        elif isinstance(node, ast.Attribute):
            target = analysis.target(node)
            if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                targets.add(target)
        if targets:
            edges.setdefault(enclosing(node), set()).update(targets)
    live: set[Definition] = set()
    pending = list(edges[None])
    while pending:
        function = pending.pop()
        if function not in live:
            live.add(function)
            pending.extend(edges.get(function, ()))
    dead = candidates - live
    if not dead:
        return

    def path(node: ast.AST) -> tuple[str, ...]:
        parts: list[str] = []
        while node in analysis.parents:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                parts.append(cast(str, node.__dict__.get("original_name", node.name)))
            node = analysis.parents[node]
        return tuple(reversed(parts))

    symbols: list[DeadSymbol] = []
    removed = {child for function in dead for child in ast.walk(function)}
    for scope in analysis.nodes:
        for binding in analysis.node_bindings(scope):
            recorded_store = False
            for definition in analysis.definitions(binding):
                if definition not in removed:
                    continue
                kind: str
                lexical: tuple[str, ...]
                if isinstance(definition, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "c" if isinstance(definition, ast.ClassDef) else "f"
                    lexical = path(definition)
                elif isinstance(definition, ast.arg):
                    kind = "a"
                    lexical = (
                        *path(scope),
                        cast(str, definition.__dict__.get("original_name", definition.arg)),
                    )
                elif isinstance(definition, ast.Name) and isinstance(definition.ctx, ast.Store):
                    if recorded_store:
                        continue
                    recorded_store = True
                    kind = "v"
                    lexical = (
                        *path(scope),
                        cast(str, definition.__dict__.get("original_name", definition.id)),
                    )
                else:
                    continue
                symbols.append(
                    (kind, lexical, cast(str | None, definition.__dict__.get("original_file")))
                )
    tree.__dict__["dead_symbols"] = symbols

    class RemoveDead(ast.NodeTransformer):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST | None:
            return None if node in dead else self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST | None:
            return None if node in dead else self.generic_visit(node)

        def generic_visit(self, node: ast.AST) -> ast.AST:
            suites = [
                field
                for field in ("body", "finalbody")
                if isinstance(node.__dict__.get(field), list) and node.__dict__[field]
            ]
            super().generic_visit(node)
            # Any suite can lose its only statement (if, try, match, class, ...).
            for field in suites:
                body = cast(list[ast.stmt], node.__dict__[field])
                if not body:
                    body.append(ast.copy_location(ast.Pass(), node))
            return node

    RemoveDead().visit(tree)
    ast.fix_missing_locations(tree)

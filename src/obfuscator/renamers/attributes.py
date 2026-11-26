from __future__ import annotations

import ast
import builtins
import inspect
import threading
from _thread import RLock  # ruff: ignore[import-private-name] - Inspect the native type without constructing a lock.
from heapq import merge
from operator import itemgetter
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from ..analysis import inspects_frames
from ..debug import record_attribute

if TYPE_CHECKING:
    from ..analysis import Binding
    from .allowlist import AllowlistRenamer

__all__: list[str] = []


def ordinary_data(analysis: AllowlistRenamer, value: ast.expr, seen: frozenset[ast.AST] = frozenset()) -> bool:
    """Prove a class default cannot implement the descriptor protocol, without calling it."""
    if value in seen:
        return False
    seen |= {value}
    if isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return True
    if isinstance(value, ast.IfExp):
        return ordinary_data(analysis, value.body, seen) and ordinary_data(analysis, value.orelse, seen)
    if isinstance(value, (ast.Name, ast.Attribute)):
        binding = analysis.bindings.get(value)
        if isinstance(value, ast.Attribute):
            owner = analysis.target(value.value, instances=False)
            if isinstance(owner, ast.ClassDef):
                binding = analysis.member(owner, value.attr)
        definitions = analysis.definitions(binding) if binding is not None else []
        if not definitions:
            return False
        return all(
            isinstance(definition, ast.Name)
            and isinstance(parent := analysis.parents.get(definition), (ast.Assign, ast.AnnAssign))
            and parent.value is not None and ordinary_data(analysis, parent.value, seen)
            for definition in definitions
        )
    if not isinstance(value, ast.Call):
        return False
    target = analysis.target(value.func, instances=False)
    if isinstance(target, ast.ClassDef):
        for base in analysis.mro(target):
            if base.keywords or base.decorator_list or any(
                binding.name in {"__get__", "__set__", "__delete__", "__set_name__", "__new__"}
                for binding in analysis.node_bindings(base)
            ):
                return False
            for parent in base.bases:
                if not isinstance(analysis.target(parent, instances=False), ast.ClassDef):
                    try:
                        external = analysis.external_base(parent)
                    except (ImportError, AttributeError, ValueError):
                        return False
                    if external is not object:
                        return False
        return True
    binding = analysis.bindings.get(value.func)
    constructor: object = None
    if isinstance(cast(object, binding), BuiltinBinding):
        constructor = vars(builtins).get(getattr(binding, "name", ""))
    else:
        try:
            path = analysis.imported_path(value.func)
            constructor = analysis.attribute_flow.external_returns.resolve(path) if path is not None else None
        except (ImportError, AttributeError, ValueError):
            return False
    if constructor is threading.RLock:
        constructor = RLock
    if not isinstance(constructor, type) or type(constructor) is not type:
        return False
    # A custom allocator can return an arbitrary object instead of an instance
    # of the called class. Only the inherited object allocator and known builtin
    # value constructors establish the result type without executing code.
    if inspect.getattr_static(constructor, "__new__") is not inspect.getattr_static(object, "__new__") and constructor not in {
        object, list, tuple, set, frozenset, dict, str, bytes, bytearray, int, float, complex, bool, RLock,
    }:
        return False
    return all(inspect.getattr_static(constructor, name, None) is None for name in ("__get__", "__set__", "__delete__", "__set_name__"))


def attribute_owners(
    analysis: AllowlistRenamer, entries: list[str],
) -> list[tuple[str, ast.ClassDef, str]]:
    """Resolve class selectors before constructing the whole-program flow graph."""
    result: list[tuple[str, ast.ClassDef, str]] = []
    if not entries:
        return result
    owners_by_path: dict[tuple[str, ...], list[ast.ClassDef]] = {}
    for owner in analysis.classes:
        owner_path = analysis.path(owner)
        owners_by_path.setdefault(owner_path, []).append(owner)
        source = owner.__dict__.get("original_file")
        if isinstance(source, str):
            owners_by_path.setdefault((source, *owner_path), []).append(owner)
    for entry in entries:
        _, *path, name = entry.split(":")
        owners = owners_by_path.get(tuple(path), [])
        if len(owners) != 1:
            raise ValueError(f"Allowlist {entry!r}: {'ambiguous target' if owners else 'target not found'}")
        if name.startswith("__"):
            raise ValueError(f"Allowlist {entry!r}: special/name-mangled attributes are unsupported")
        result.append((entry, owners[0], name))
    return result


def plan_attributes(
    analysis: AllowlistRenamer, entries: list[str]
) -> tuple[dict[ast.Attribute | ast.Constant, str], dict[Binding, str], dict[str, str]]:
    """Resolve a complete attribute transaction before changing the staging tree."""
    changes: dict[ast.Attribute | ast.Constant, str] = {}
    bindings: dict[Binding, str] = {}
    report: dict[str, str] = {}
    if not entries:
        return changes, bindings, report
    targets = attribute_owners(analysis, entries)
    nodes = analysis.nodes
    if inspects_frames(nodes) or getattr(analysis.tree, "tainted", False):
        raise ValueError("Cannot rename attributes in a reflective namespace")
    flow = analysis.attribute_flow
    analysis.used.update(analysis.strings)
    # A family's first field needs the full storage/escape check. Subsequent
    # fields only need matching attributes and builtin reflection calls. Index
    # those once, preserving tree order (and therefore fail-closed error order).
    reflective = {
        "__dict__", "__class__", "__getattribute__", "__setattr__", "__delattr__",
        "__dir__", "__reduce__", "__reduce_ex__",
    }
    common: list[tuple[int, ast.AST]] = []
    named: dict[str, list[tuple[int, ast.AST]]] = {}
    for index, node in enumerate(nodes):
        if id(node) in analysis.annotation_nodes:
            continue
        if isinstance(node, ast.Attribute):
            if node.attr in reflective:
                common.append((index, node))
            else:
                named.setdefault(node.attr, []).append((index, node))
        elif isinstance(node, ast.Call) and node.args:
            builtin = analysis.bindings.get(node.func)
            if isinstance(cast(object, builtin), BuiltinBinding) and getattr(builtin, "name", "") in {
                "getattr", "setattr", "delattr", "hasattr", "vars", "dir"
            }:
                common.append((index, node))
    for node in nodes:
        analysis.used.update(binding.name for binding in analysis.node_bindings(node))
        if isinstance(node, ast.Attribute):
            analysis.used.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            analysis.used.add(node.arg)
    selected: dict[tuple[ast.ClassDef, str], str] = {}
    verified_families: set[ast.ClassDef] = set()
    for entry, owner, name in targets:
        family = {cls for cls in analysis.classes if owner in analysis.mro(cls)}
        for cls in family | set(analysis.mro(owner)):
            analysis.check_receiver(cls, name)
            if cls.decorator_list or any(
                binding.name in {"__slots__", "__setattr__", "__delattr__", "__init_subclass__"}
                for binding in analysis.node_bindings(cls)
            ):
                raise ValueError(f"Allowlist {entry!r}: unsupported dynamic attribute class {cls.name!r}")
        binding = analysis.member(owner, name)
        if any(
            (id(base), name) in analysis.instance_assignments
            for base in analysis.mro(owner)[1:]
        ):
            raise ValueError(f"Allowlist {entry!r}: inherited attribute conflict")
        if binding is not None:
            if binding not in analysis.node_bindings(owner) or any(
                not isinstance(ref, (ast.Name, ast.Global, ast.Nonlocal))
                for ref in analysis.definitions(binding)
            ):
                raise ValueError(f"Allowlist {entry!r}: inherited member or descriptor conflict")
            for ref in analysis.definitions(binding):
                parent = analysis.parents.get(ref)
                if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
                    if not ordinary_data(analysis, parent.value):
                        raise ValueError(f"Allowlist {entry!r}: unsupported descriptor value")
        for cls in family - {owner}:
            if any(item.name == name for item in analysis.node_bindings(cls)) or (
                id(cls), name
            ) in analysis.instance_assignments:
                raise ValueError(f"Allowlist {entry!r}: conflicting subclass attribute")
        references: list[ast.Attribute | ast.Constant] = []
        # Escape checks depend on the receiver family, not the selected field.
        # Several fields of the same class can share that whole-tree check.
        verify_family = owner not in verified_families
        if verify_family:
            flow.prepare_contexts(family)
        candidates = nodes if verify_family else (
            node for _, node in merge(common, named.get(name, ()), key=itemgetter(0))
        )
        for node in candidates:
            if id(node) in analysis.annotation_nodes:
                continue
            if verify_family:
                flow.verify_storage(node, family)
            if isinstance(node, ast.Attribute) and node.attr == name:
                receivers = flow.receivers(node.value)
                if not receivers and not flow.unrelated(node.value, family):
                    raise ValueError(f"Unresolved receiver for allowlisted attribute {flow.describe(node)}")
                if receivers & family:
                    if not receivers <= family or flow.value(node.value).unknown:
                        raise ValueError(f"Ambiguous polymorphic attribute {ast.unparse(node)!r}")
                    references.append(node)
            if isinstance(node, ast.Attribute) and node.attr in reflective:
                receivers = flow.receivers(node.value)
                if receivers & family:
                    raise ValueError(f"Reflective access to allowlisted attribute class: {ast.unparse(node)!r}")
            if not isinstance(node, ast.Call):
                continue
            builtin = analysis.bindings.get(node.func)
            builtin_name = getattr(builtin, "name", "")
            if isinstance(cast(object, builtin), BuiltinBinding) and builtin_name in {
                "getattr", "setattr", "delattr", "hasattr", "vars", "dir"
            } and node.args:
                receivers = flow.receivers(node.args[0])
                key = node.args[1] if len(node.args) > 1 else None
                literal = isinstance(key, ast.Constant) and isinstance(key.value, str)
                if builtin_name in {"vars", "dir"} and flow.exposes(flow.value(node.args[0]), family):
                    raise ValueError(f"Dynamic reflection on allowlisted attribute: {ast.unparse(node)!r}")
                if receivers & family:
                    if builtin_name in {"vars", "dir"} or not literal:
                        raise ValueError(f"Dynamic reflection on allowlisted attribute: {ast.unparse(node)!r}")
                    if isinstance(key, ast.Constant) and key.value == name:
                        if not receivers <= family:
                            raise ValueError("Ambiguous reflective attribute receiver")
                        references.append(key)
                elif not receivers and not flow.unrelated(node.args[0], family) and (
                    not literal or (isinstance(key, ast.Constant) and key.value == name)
                ):
                    raise ValueError(f"Unresolved reflective attribute receiver: {ast.unparse(node)!r}")
                continue
            if verify_family:
                flow.verify_contextual_call(node, family)
        verified_families.add(owner)
        if binding is None and not references:
            raise ValueError(f"Allowlist {entry!r}: attribute target not found")
        identity = (owner, name)
        if identity not in selected:
            selected[identity] = analysis.fresh_name()
        replacement = selected[identity]
        if binding is not None:
            bindings[binding] = replacement
        for reference in references:
            if reference in changes and changes[reference] != replacement:
                raise ValueError(f"Allowlist {entry!r}: conflicting attribute selections")
            changes[reference] = replacement
        report[entry] = replacement
        record_attribute(owner, name, replacement)
    return changes, bindings, report

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from ..analysis import inspects_frames

if TYPE_CHECKING:
    from ..analysis import Binding
    from .allowlist import AllowlistRenamer

__all__: list[str] = []


def plan_attributes(
    analysis: AllowlistRenamer, entries: list[str]
) -> tuple[dict[ast.Attribute | ast.Constant, str], dict[Binding, str], dict[str, str]]:
    """Resolve a complete attribute transaction before changing the staging tree."""
    changes: dict[ast.Attribute | ast.Constant, str] = {}
    bindings: dict[Binding, str] = {}
    report: dict[str, str] = {}
    if not entries:
        return changes, bindings, report
    nodes = analysis.nodes
    if inspects_frames(nodes) or getattr(analysis.tree, "tainted", False):
        raise ValueError("Cannot rename attributes in a reflective namespace")
    flow = analysis.attribute_flow
    analysis.used.update(analysis.strings)
    for node in nodes:
        analysis.used.update(binding.name for binding in analysis.node_bindings(node))
        if isinstance(node, ast.Attribute):
            analysis.used.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            analysis.used.add(node.arg)
    selected: dict[tuple[ast.ClassDef, str], str] = {}
    verified_families: set[ast.ClassDef] = set()
    for entry in entries:
        _, *path, name = entry.split(":")
        source = path.pop(0) if path[0].endswith(".py") else None
        owners = [
            owner for owner in analysis.classes
            if analysis.path(owner) == tuple(path)
            and (source is None or owner.__dict__.get("original_file") == source)
        ]
        if len(owners) != 1:
            raise ValueError(f"Allowlist {entry!r}: {'ambiguous target' if owners else 'target not found'}")
        owner = owners[0]
        if name.startswith("__"):
            raise ValueError(f"Allowlist {entry!r}: special/name-mangled attributes are unsupported")
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
                    if not isinstance(parent.value, ast.Constant):
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
        for node in nodes:
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
            if isinstance(node, ast.Attribute) and node.attr in {
                "__dict__", "__class__", "__getattribute__", "__setattr__", "__delattr__",
                "__dir__", "__reduce__", "__reduce_ex__",
            }:
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
                flow.verify_call(node, family)
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
    return changes, bindings, report

from __future__ import annotations

import ast
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from python_minifier.rename.resolve_names import get_binding

from ..analysis import Binding
from .base import ArgumentRenamer

if TYPE_CHECKING:
    from ..analysis import Function
    from .base import CallAnalysis

__all__: list[str] = [
    "PrivateMethodRenamer",
    "rename_constructor_classes",
    "rename_private_classes",
]


def rename_constructor_classes(tree: ast.Module) -> None:
    """Shorten lexical class bindings used exclusively for construction.

    Retained runtime annotations count as non-constructor references too: their
    names may be resolved later by dacite or typing, outside lexical binding.
    """
    analysis = ArgumentRenamer({"__main__": tree})
    if analysis.frame_inspection or getattr(tree, "tainted", False):
        return
    nodes = analysis.nodes
    analysis.used.update(analysis.strings)
    quoted_names: set[str] = set()
    reflected_classes: set[ast.ClassDef] = set()
    for node in nodes:
        analysis.used.update(binding.name for binding in analysis.node_bindings(node))
        if isinstance(node, ast.Attribute):
            analysis.used.add(node.attr)
            if node.attr in {"__class__", "__name__", "__qualname__"}:
                reflected_classes.update(analysis.type_targets(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "type"
            and len(node.args) == 1
        ):
            reflected_classes.update(analysis.type_targets(node.args[0]))
        # A string forward annotation is not a lexical binding reference.
        if isinstance(node, ast.AnnAssign):
            pending: list[ast.AST] = [node.annotation]
            while pending:
                for part in ast.walk(pending.pop()):
                    if isinstance(part, ast.Name):
                        quoted_names.add(part.id)
                    elif isinstance(part, ast.Constant) and isinstance(part.value, str):
                        with suppress(SyntaxError):
                            pending.append(ast.parse(part.value, mode="eval"))
    for node in nodes:
        if (
            not isinstance(node, ast.ClassDef)
            or node.__dict__.get("no_rename")
            or node in reflected_classes
            or node.name in quoted_names
            or node.name != node.__dict__.get("original_name", node.name)
            or isinstance(node.__dict__.get("namespace"), ast.ClassDef)
            or node.bases
            or node.keywords
            or not analysis.safe_class_decorators(node)
        ):
            continue
        # Python derives private member/parameter spellings from the class name.
        if any(
            name.startswith("__") and not name.endswith("__")
            for member in ast.walk(node)
            for name in (
                member.id
                if isinstance(member, ast.Name)
                else member.arg
                if isinstance(member, ast.arg)
                else member.attr
                if isinstance(member, ast.Attribute)
                else member.name
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else member.value
                if isinstance(member, ast.Constant) and isinstance(member.value, str)
                else "",
            )
        ):
            continue
        binding = analysis.bindings.get(node)
        if binding is None or analysis.definitions(binding) != [node]:
            continue
        references = [reference for reference in binding.references if reference is not node]
        if not references or not all(
            isinstance(reference, ast.Name)
            and isinstance(reference.ctx, ast.Load)
            and isinstance(parent := analysis.parents.get(reference), ast.Call)
            and parent.func is reference
            for reference in references
        ):
            continue
        name = analysis.fresh_name()
        if len(name) < len(node.name):
            binding.rename(name)


class PrivateMethodRenamer(ArgumentRenamer):
    """Shorten closed single-underscore methods and their resolved references.

    Unlike parameter renaming, a dynamic **kwargs call is harmless here. Method
    names still stay intact for escaping receivers/callbacks, reflective access,
    inheritance, unknown decorators, and unresolved same-named attributes.
    """

    def __init__(self, trees: dict[str, ast.Module]) -> None:
        super().__init__(trees)
        self._method_analysis: CallAnalysis | None = None

    def rename_parameters(self, *, reuse_calls: bool = False) -> None:
        """Rename parameters, optionally reusing the private-method call analysis.

        Fresh names cannot collide with existing names or reflected strings, so
        method renaming changes neither call targets nor their escape status.
        Reuse requires that no other edits occurred since ``run``. The base
        pass still clears spelling-dependent inference caches.
        """
        analysis = self._method_analysis if reuse_calls else None
        self._method_analysis = None
        self._run(analysis)

    def run(self) -> None:
        self._class_mros.clear()
        self._member_bindings.clear()
        self.imported_paths.clear()
        self.renamed = 0
        self._method_analysis = self.analyze_calls()
        _, _, reflected = self._method_analysis
        references: dict[Function, list[ast.Attribute]] = {}
        candidates = [
            function
            for function in self.functions
            if not isinstance(function, ast.Lambda)
            and not function.__dict__.get("no_rename")
            and function.name.startswith("_")
            and not function.name.startswith("__")
            and function not in reflected
            and not self.is_property(function)
            and isinstance(self.parents.get(function), ast.ClassDef)
        ]
        names = {function.name for function in candidates}
        # Reserve attributes too: a new method must never shadow instance data,
        # another class member, or an attribute queried by its literal name.
        self.used.update(self.strings)
        for nodes in self.module_nodes.values():
            for node in nodes:
                if id(node) in self.annotation_nodes:
                    continue
                self.used.update(binding.name for binding in self.node_bindings(node))
                if isinstance(node, ast.Attribute):
                    self.used.add(node.attr)
                    if node.attr not in names:
                        continue
                    target = self.target(node)
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        references.setdefault(target, []).append(node)
                elif isinstance(node, ast.Name):
                    self.used.add(node.id)
                elif isinstance(node, ast.arg):
                    self.used.add(node.arg)
        # Resolve every attribute before changing any definition or binding.
        for function in candidates:
            binding = self.bindings.get(function)
            if binding is None or self.definitions(binding) != [function]:
                continue
            new = self.fresh_name()
            binding.rename(new)
            for reference in references.get(function, []):
                reference.attr = new
            self.renamed += 1
        if self.renamed:
            self.imported_paths.clear()


def rename_private_classes(tree: ast.Module) -> None:
    """Shorten private classes after internal imports have been linked."""
    analysis = ArgumentRenamer({"__main__": tree})
    used: set[str] = set()
    for node in analysis.nodes:
        used.update(binding.name for binding in analysis.node_bindings(node))
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.arg):
            used.add(node.arg)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    used.update(analysis.strings)
    annotations: list[tuple[ast.Constant, ast.Expression, str]] = []

    def bind_annotation(node: ast.AST, namespace: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                expression = ast.parse(node.value, mode="eval")
            except SyntaxError:
                return
            bind_annotation(expression, namespace)
            annotations.append((node, expression, ast.dump(expression)))
        elif isinstance(node, ast.Name):
            binding = cast(Callable[[str, ast.AST], Binding], get_binding)(node.id, namespace)
            binding.references.append(node)
        else:
            for child in ast.iter_child_nodes(node):
                bind_annotation(child, namespace)

    for node in analysis.nodes:
        if isinstance(node, ast.AnnAssign):
            bind_annotation(node.annotation, cast(ast.AST, node.__dict__["namespace"]))
    number = 0
    members: dict[ast.ClassDef, tuple[str, str]] = {}
    renamed: set[int] = set()
    for node in analysis.nodes:
        if (
            not isinstance(node, ast.ClassDef)
            or node.__dict__.get("no_rename")
            or not node.name.startswith("_")
        ):
            continue
        binding = analysis.bindings.get(node)
        if binding is None or id(binding) in renamed:
            continue
        while f"_g{number}" in used:
            number += 1
        name = f"_g{number}"
        used.add(name)
        if isinstance(node.__dict__["namespace"], ast.ClassDef):
            # Attributes can be accessed dynamically or through subclasses.
            # Minify the class itself, but retain its attribute binding.
            members[node] = (node.name, name)
            node.name = name
        else:
            binding.rename(name)
            renamed.add(id(binding))
    for constant, expression, original in annotations:
        if ast.dump(expression) != original:
            constant.value = ast.unparse(expression.body)

    class MemberAliases(ast.NodeTransformer):
        def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST | list[ast.stmt]:
            self.generic_visit(node)
            if node not in members:
                return node
            original, alias = members[node]
            target: ast.Name = ast.Name(id=original, ctx=ast.Store())
            reference: ast.Name = ast.Name(id=alias, ctx=ast.Load())
            cleanup: ast.Name = ast.Name(id=alias, ctx=ast.Del())
            node.__dict__["class_alias_target"] = target
            node.__dict__["class_alias_references"] = [reference, cleanup]
            return [
                node,
                ast.Assign(targets=[target], value=reference),
                ast.Delete(targets=[cleanup]),
            ]

    MemberAliases().visit(tree)
    ast.fix_missing_locations(tree)

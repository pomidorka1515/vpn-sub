from __future__ import annotations

import ast
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from python_minifier.rename.resolve_names import get_binding

from ..analysis import Binding
from .base import ArgumentRenamer

if TYPE_CHECKING:
    from ..analysis import Function

__all__: list[str] = ["PrivateMethodRenamer", "rename_private_classes"]


class PrivateMethodRenamer(ArgumentRenamer):
    """Shorten closed single-underscore methods and their resolved references.

    Unlike parameter renaming, a dynamic **kwargs call is harmless here. Method
    names still stay intact for escaping receivers/callbacks, reflective access,
    inheritance, unknown decorators, and unresolved same-named attributes.
    """

    def run(self) -> None:
        self.renamed = 0
        _, _, reflected = self.analyze_calls()
        references: dict[Function, list[ast.Attribute]] = {}
        # Reserve attributes too: a new method must never shadow instance data,
        # another class member, or an attribute queried by its literal name.
        self.used.update(self.strings)
        for tree in self.trees.values():
            for node in ast.walk(tree):
                if id(node) in self.annotation_nodes:
                    continue
                self.used.update(binding.name for binding in self.node_bindings(node))
                if isinstance(node, ast.Attribute):
                    self.used.add(node.attr)
                    target = self.target(node)
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        references.setdefault(target, []).append(node)
                elif isinstance(node, ast.Name):
                    self.used.add(node.id)
                elif isinstance(node, ast.arg):
                    self.used.add(node.arg)
        # Resolve every attribute before changing any definition or binding.
        for function in self.functions:
            if (
                isinstance(function, ast.Lambda)
                or not function.name.startswith("_")
                or function.name.startswith("__")
                or function in reflected
                or self.is_property(function)
                or not isinstance(self.parents.get(function), ast.ClassDef)
            ):
                continue
            binding = self.bindings.get(function)
            if binding is None or self.definitions(binding) != [function]:
                continue
            new = self.fresh_name()
            binding.rename(new)
            for reference in references.get(function, []):
                reference.attr = new
            self.renamed += 1


def rename_private_classes(tree: ast.Module) -> None:
    """Shorten private classes after internal imports have been linked."""
    analysis = ArgumentRenamer({"__main__": tree})
    used = {binding.name for node in ast.walk(tree) for binding in analysis.node_bindings(node)}
    used.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    used.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    used.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
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

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            bind_annotation(node.annotation, cast(ast.AST, node.__dict__["namespace"]))
    number = 0
    members: dict[ast.ClassDef, tuple[str, str]] = {}
    renamed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("_"):
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

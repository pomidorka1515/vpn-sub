from __future__ import annotations

import ast
from collections import Counter
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from .renamers.base import ArgumentRenamer
from .renamers.methods import rename_private_classes

if TYPE_CHECKING:
    from pathlib import Path

    from .analysis import Binding

__all__: list[str] = ["flatten", "module_name"]


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def flatten(trees: dict[str, ast.Module], *, rename_classes: bool = True) -> ast.Module:
    """Link application bindings directly into one global scope, like includes."""
    analysis = ArgumentRenamer(trees)
    # Retain source spellings for allowlist selection after collision linking.
    for module, tree in trees.items():
        source_file = module.replace(".", "/")
        source_file += "/__init__.py" if tree.__dict__.get("is_package") else ".py"
        for node in ast.walk(tree):
            node.__dict__.setdefault("original_file", source_file)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node.__dict__.setdefault("original_name", node.name)
            elif isinstance(node, ast.Name):
                node.__dict__.setdefault("original_name", node.id)
            elif isinstance(node, ast.arg):
                node.__dict__.setdefault("original_name", node.arg)
    public_classes = {
        node: node.name
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    }
    used = {
        node.id for tree in trees.values() for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    used.update(
        node.arg for tree in trees.values() for node in ast.walk(tree) if isinstance(node, ast.arg)
    )
    used.update(
        binding.name
        for tree in trees.values()
        for node in ast.walk(tree)
        for binding in analysis.node_bindings(node)
    )

    def fresh(prefix: str) -> str:
        number = 0
        while f"{prefix}{number}" in used:
            number += 1
        name = f"{prefix}{number}"
        used.add(name)
        return name

    names: dict[int, str] = {}
    external: dict[tuple[str, str | None, str], str] = {}
    # Only actual builtin references reserve builtin names. Re-exports and
    # import aliases are not additional application definitions.
    claimed: dict[str, object] = {
        binding.name: "builtin"
        for symbols in analysis.symbols.values()
        for binding in symbols.values()
        if isinstance(cast(object, binding), BuiltinBinding)
    }
    definitions = Counter(
        name
        for symbols in analysis.symbols.values()
        for name, binding in symbols.items()
        if not isinstance(cast(object, binding), BuiltinBinding)
        and not any(isinstance(ref, ast.alias) for ref in binding.references)
    )
    # A colliding public class keeps its class statement's name, using a
    # temporary global slot and a distinct linked alias. Reserve that slot so
    # defining the class cannot overwrite another module's live binding.
    for name in public_classes.values():
        if definitions[name] > 1:
            claimed[name] = "public class slot"
    imports: list[ast.stmt] = []
    dependencies: dict[str, list[str]] = {module: [] for module in trees}

    def library(module: str, name: str | None, preferred: str | None = None) -> str:
        preferred = preferred or name or module.split(".", maxsplit=1)[0]
        key = (module, name, preferred)
        if key not in external:
            identity = (module, name)
            alias = (
                preferred
                if preferred not in claimed or claimed[preferred] == identity
                else fresh("_e")
            )
            claimed[alias] = identity
            external[key] = alias
            if name is None:
                imports.append(ast.Import(names=[ast.alias(name=module, asname=alias)]))
            else:
                imports.append(
                    ast.ImportFrom(
                        module=module, names=[ast.alias(name=name, asname=alias)], level=0
                    )
                )
        return external[key]

    # Allocate ordinary definitions first; imports and re-exports point to these
    # same names rather than creating copies or emulated module objects.
    for symbols in analysis.symbols.values():
        for name, binding in symbols.items():
            if isinstance(cast(object, binding), BuiltinBinding):
                continue
            if any(isinstance(ref, ast.alias) for ref in binding.references):
                continue
            chosen = name if name not in claimed else fresh("_g")
            names[id(binding)] = chosen
            claimed[chosen] = id(binding)

    def imported(module: str, name: str | None, seen: set[int]) -> tuple[str, bool]:
        if module not in trees:
            return library(module, name), False
        if name is None:
            return module, True
        binding = analysis.symbols[module].get(name)
        if binding is not None:
            return resolve(binding, seen)
        child = f"{module}.{name}"
        if child in trees:
            return child, True
        raise ValueError(f"Cannot flatten unresolved internal import: {module}.{name}")

    def resolve(binding: Binding, seen: set[int]) -> tuple[str, bool]:
        if id(binding) in names:
            return names[id(binding)], False
        if id(binding) in seen:
            raise ValueError(f"Circular re-export: {binding.name}")
        aliases = [ref for ref in binding.references if isinstance(ref, ast.alias)]
        if len(aliases) != 1:
            raise ValueError(f"Cannot flatten ambiguous import binding: {binding.name}")
        module, name = analysis.imports[aliases[0]]
        if module not in trees:
            return library(module, name, binding.name), False
        return imported(module, name, seen | {id(binding)})

    def reference(node: ast.expr) -> tuple[str, bool] | None:
        if isinstance(node, ast.Name):
            binding = analysis.bindings.get(node)
            if binding is not None and (
                id(binding) in names
                or any(isinstance(ref, ast.alias) for ref in binding.references)
            ):
                return resolve(binding, set())
        elif isinstance(node, ast.Attribute):
            owner = reference(node.value)
            if owner is not None and owner[1]:
                return imported(owner[0], node.attr, set())
        return None

    # Removing ``worker.`` can expose a real collision with a caller's local
    # ``calculate``. Rename the linked symbol only when such capture occurs.
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            target = reference(node)
            if target is None or target[1]:
                continue
            name = target[0]
            initial = cast(ast.AST, node.__dict__["namespace"])
            scope = initial
            while not isinstance(scope, ast.Module):
                bindings = (
                    analysis.node_bindings(scope)
                    if not isinstance(scope, ast.ClassDef) or scope is initial
                    else []
                )
                shadow = next((binding for binding in bindings if binding.name == name), None)
                # Pure import bindings disappear during linking, so cannot
                # capture a global even when their original aliases match.
                if shadow is not None and any(
                    not isinstance(definition, ast.alias)
                    for definition in analysis.definitions(shadow)
                ):
                    replacement = fresh("_g" if name in names.values() else "_e")
                    names.update(
                        (key, replacement) for key, value in names.items() if value == name
                    )
                    external.update(
                        (key, replacement) for key, value in external.items() if value == name
                    )
                    for statement in imports:
                        for alias in cast(ast.Import | ast.ImportFrom, statement).names:
                            if alias.asname == name:
                                alias.asname = replacement
                    break
                scope = cast(ast.AST, scope.__dict__["namespace"])

    replacements: dict[ast.AST, ast.expr] = {}
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        raise ValueError("Star imports cannot be flattened; use explicit imports")
                    import_module, import_name = analysis.imports[alias]
                    if import_module == "__future__":
                        continue
                    if import_module in trees:
                        dependencies[module].append(import_module)
                        if isinstance(node, ast.Import) and alias.name in trees:
                            dependencies[module].append(alias.name)
                        child = f"{import_module}.{import_name}" if import_name else ""
                        if child in trees:
                            dependencies[module].append(child)
                    else:
                        preferred = alias.asname or (
                            alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                        )
                        library(import_module, import_name, preferred)
                        if isinstance(node, ast.Import) and not alias.asname and "." in alias.name:
                            # ``import a.b`` binds a but must also load a.b.
                            library(alias.name, None)
            elif isinstance(node, (ast.Name, ast.Attribute)):
                target = reference(node)
                if target is not None:
                    if target[1]:
                        parent = analysis.parents.get(node)
                        if not isinstance(parent, ast.Attribute) or parent.value is not node:
                            raise ValueError(f"Internal module used as a value: {target[0]}")
                    else:
                        linked_reference = ast.copy_location(
                            ast.Name(id=target[0], ctx=node.ctx), node
                        )
                        if "original_name" in node.__dict__:
                            linked_reference.__dict__["original_name"] = node.__dict__[
                                "original_name"
                            ]
                        if "original_file" in node.__dict__:
                            linked_reference.__dict__["original_file"] = node.__dict__["original_file"]
                        replacements[node] = linked_reference

    # Update global declarations and definitions as well as lexical references.
    for symbols in analysis.symbols.values():
        for binding in symbols.values():
            if id(binding) in names and binding.name != names[id(binding)]:
                binding.rename(names[id(binding)])

    class Link(ast.NodeTransformer):
        def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST | list[ast.stmt]:
            self.generic_visit(node)
            original = public_classes.get(node)
            if original is None or original == node.name:
                return node
            # Keep the class statement's name (including for decorators,
            # metaclasses and nested qualnames), then move its binding to the
            # collision-free alias used by linked references.
            alias = node.name
            node.name = original
            target: ast.Name = ast.Name(id=alias, ctx=ast.Store())
            reference: ast.Name = ast.Name(id=original, ctx=ast.Load())
            cleanup: ast.Name = ast.Name(id=original, ctx=ast.Del())
            node.__dict__["class_alias_target"] = target
            node.__dict__["class_alias_references"] = [reference, cleanup]
            return [
                node,
                ast.Assign(targets=[target], value=reference),
                ast.Delete(targets=[cleanup]),
            ]

        def visit_Name(self, node: ast.Name) -> ast.AST:
            return replacements.get(node, node)

        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            return replacements.get(node) or self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            return None

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            return None

        def visit_For(self, node: ast.For) -> ast.AST:
            # The entrypoint's probe imports every internal module by string.
            # Those modules are already linked; retain only its external checks.
            if isinstance(node.iter, ast.Tuple) and any(
                isinstance(child, ast.Call)
                and reference(child.func)
                in {
                    (alias, False)
                    for (module, name, _), alias in external.items()
                    if (module, name) == ("importlib", "import_module")
                }
                for statement in node.body
                for child in ast.walk(statement)
            ):
                node.iter.elts = [
                    value
                    for value in node.iter.elts
                    if not (
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value in trees
                    )
                ]
            return self.generic_visit(node)

    # Runtime dataclass annotations can be quoted forward references. Rewrite
    # those using their original module's globals, not unrelated string values.
    for module, tree in trees.items():

        class ForwardReferences(ast.NodeTransformer):
            def __init__(self, module: str) -> None:
                self.module = module

            def visit_Constant(self, node: ast.Constant) -> ast.AST:
                if not isinstance(node.value, str):
                    return node
                try:
                    expression = ast.parse(node.value, mode="eval")
                except SyntaxError:
                    return node
                self.visit(expression)
                node.value = ast.unparse(expression.body)
                return node

            def visit_Name(self, node: ast.Name) -> ast.AST:
                binding = analysis.symbols[self.module].get(node.id)
                if binding is not None and not isinstance(cast(object, binding), BuiltinBinding):
                    name, is_module = resolve(binding, set())
                    if not is_module:
                        node.id = name
                return node

        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                ForwardReferences(module).visit(node.annotation)

    body: list[ast.stmt] = []
    visited: set[str] = set()

    def include(module: str) -> None:
        if module in visited:
            return
        visited.add(module)
        for dependency in dependencies[module]:
            include(dependency)
        linked = cast(ast.Module, Link().visit(trees[module]))
        # Non-entry modules must never run their script guards in the new scope.
        for node in linked.body:
            if (
                module != "main"
                and isinstance(node, ast.If)
                and ast.unparse(node.test) == "__name__ == '__main__'"
            ):
                body.extend(node.orelse)
            else:
                body.append(node)

    for module in trees:
        if module != "main":
            include(module)
    if "main" in trees:
        include("main")
    result = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *imports,
            *body,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(result)
    for node in ast.walk(result):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.__dict__["linked_name"] = node.name
        elif isinstance(node, ast.Name):
            node.__dict__["linked_name"] = node.id
        elif isinstance(node, ast.arg):
            node.__dict__["linked_name"] = node.arg
    if rename_classes:
        rename_private_classes(result)
    return result

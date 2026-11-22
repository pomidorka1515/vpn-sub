from __future__ import annotations

import argparse
import ast
import builtins
import keyword
import os
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol, cast

import python_minifier
from python_minifier.ast_annotation import add_parent
from python_minifier.rename.bind_names import bind_names
from python_minifier.rename.binding import BuiltinBinding
from python_minifier.rename.mapper import add_namespace
from python_minifier.rename.resolve_names import resolve_names

type Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def inspects_frames(tree: ast.Module) -> bool:
    """Frame/stack APIs can expose names in any active lexical scope."""
    frame_apis = {
        "_getframe",
        "_current_frames",
        "currentframe",
        "settrace",
        "setprofile",
        "f_locals",
        "f_globals",
        "co_varnames",
    }
    return any(
        (isinstance(node, ast.Attribute) and node.attr in frame_apis)
        or (isinstance(node, ast.alias) and node.name in frame_apis)
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dir"
            and not node.args
            and not node.keywords
        )
        for node in ast.walk(tree)
    )


class Binding(Protocol):
    """The lexical binding interface provided by pinned python-minifier 3.4.0."""

    name: str
    references: list[ast.AST]

    def rename(self, new_name: str) -> None: ...


class ArgumentRenamer:
    """Rename closed, statically resolved signatures across the staging tree.

    Single-assignment aliases, instances and cross-module calls are resolved lexically.
    Closed receiver parameters and stdlib context-manager decorators are supported.
    Escaping callables, unknown decorators, inheritance, unknown method receivers and
    dynamic keyword dictionaries preserve signatures. Unrelated attribute names
    and string values do not prevent renaming a closed function's parameters.
    Variadic bindings are local names; renaming them never changes kwargs keys.
    """

    def __init__(self, trees: dict[str, ast.Module]) -> None:
        self.trees = trees
        self.parents: dict[ast.AST, ast.AST] = {}
        self.bindings: dict[ast.AST, Binding] = {}
        self.modules: dict[ast.AST, str] = {}
        self.symbols: dict[str, dict[str, Binding]] = {}
        self.imports: dict[ast.alias, tuple[str, str | None]] = {}
        self.star_imports: set[str] = set()
        self.functions: list[Function] = []
        self.used = set(keyword.kwlist) | {"self", "cls"}
        self.strings: set[str] = set()
        self.next_name = 0
        self.renamed = 0
        self.skipped: Counter[str] = Counter()
        for module, tree in trees.items():
            # Use the minifier's lexical resolver rather than text replacement:
            # closures, nonlocal, comprehensions and shadowing need real bindings.
            for operation in (add_parent, add_namespace, bind_names, resolve_names):
                cast(Callable[[ast.Module], None], operation)(tree)
            self.symbols[module] = {binding.name: binding for binding in self.node_bindings(tree)}
            for node in ast.walk(tree):
                self.modules[node] = module
                self.parents.update((child, node) for child in ast.iter_child_nodes(node))
                for binding in self.node_bindings(node):
                    self.bindings.update((ref, binding) for ref in binding.references)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    self.functions.append(node)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.strings.add(node.value)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.imports[alias] = (
                            alias.name if alias.asname else alias.name.split(".")[0],
                            None,
                        )
                elif isinstance(node, ast.ImportFrom):
                    package = module.split(".")[:-1]
                    if getattr(tree, "is_package", False):
                        package = module.split(".")
                    if node.level:
                        package = package[: len(package) - node.level + 1]
                        target = ".".join([*package, *([node.module] if node.module else [])])
                    else:
                        target = node.module or ""
                    for alias in node.names:
                        self.imports[alias] = (target, alias.name)
                        if alias.name == "*":
                            self.star_imports.add(target)

    @staticmethod
    def node_bindings(node: ast.AST) -> list[Binding]:
        return cast(list[Binding], getattr(node, "bindings", []))

    def descriptor(self, function: Function) -> str | None:
        """Recognize only unshadowed builtin method descriptors, not arbitrary decorators."""
        if isinstance(function, ast.Lambda) or len(function.decorator_list) != 1:
            return None
        decorator = function.decorator_list[0]
        if not isinstance(self.parents.get(function), ast.ClassDef) or not isinstance(
            decorator, ast.Name
        ):
            return None
        binding = self.bindings.get(decorator)
        if (
            decorator.id in {"staticmethod", "classmethod"}
            and isinstance(cast(object, binding), BuiltinBinding)
            and binding is not None
            and all(
                isinstance(reference, ast.Name) and isinstance(reference.ctx, ast.Load)
                for reference in binding.references
            )
        ):
            return decorator.id
        return None

    def alias_assignment(self, node: ast.AST) -> ast.Name | None:
        """Return the destination of a lexically single-assignment alias."""
        assignment = self.parents.get(node)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is not node:
            return None
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return None
        destination = targets[0]
        target = self.target(node)
        return destination if target is not None and self.target(destination) is target else None

    def forwarding_decorators(self, function: Function) -> bool:
        """Trust only imported stdlib decorators that forward the original arguments."""
        if isinstance(function, ast.Lambda):
            return False
        supported = {"contextmanager", "asynccontextmanager"}
        for decorator in function.decorator_list:
            reference = decorator.value if isinstance(decorator, ast.Attribute) else decorator
            binding = self.bindings.get(reference)
            definitions = self.definitions(binding) if binding is not None else []
            if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
                return False
            module, name = self.imports.get(definitions[0], ("", None))
            if isinstance(decorator, ast.Attribute):
                if name is not None:
                    return False
                name = decorator.attr
            if module != "contextlib" or module in self.trees or name not in supported:
                return False
        return bool(function.decorator_list)

    @staticmethod
    def definitions(binding: Binding) -> list[ast.AST]:
        return [
            ref
            for ref in binding.references
            if not isinstance(ref, (ast.Name, ast.Global, ast.Nonlocal))
            or (isinstance(ref, ast.Name) and not isinstance(ref.ctx, ast.Load))
        ]

    def imported_attribute(self, module: str, attribute: ast.Attribute) -> bool:
        """An external re-export is not a reflective use of its containing module."""
        if not isinstance(attribute.ctx, ast.Load) or attribute.attr.startswith("__"):
            return False
        binding = self.symbols.get(module, {}).get(attribute.attr)
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
            return False
        imported_module, _ = self.imports.get(definitions[0], ("", None))
        return bool(imported_module) and imported_module not in self.trees

    def binding_target(
        self, binding: Binding | None, seen: set[int], *, instances: bool = True
    ) -> ast.AST | str | None:
        if binding is None or id(binding) in seen:
            return None
        seen = seen.union({id(binding)})
        definitions = self.definitions(binding)
        if len(definitions) != 1:
            return None
        definition = definitions[0]
        if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return definition
        if isinstance(definition, ast.Name) and isinstance(definition.ctx, ast.Store):
            assignment = self.parents.get(definition)
            if (
                isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and (not isinstance(assignment, ast.Assign) or assignment.targets == [definition])
                and isinstance(assignment.value, (ast.Name, ast.Attribute, ast.Lambda))
            ):
                return self.target(assignment.value, seen, instances=instances)
            if (
                instances
                and isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and (not isinstance(assignment, ast.Assign) or assignment.targets == [definition])
                and isinstance(assignment.value, ast.Call)
            ):
                owner = self.target(assignment.value.func, seen, instances=False)
                # Only ordinary constructors are known to return an instance
                # of their class. Factories, metaclasses and __new__ may not.
                if isinstance(owner, ast.ClassDef) and not (
                    owner.bases
                    or owner.keywords
                    or owner.decorator_list
                    or any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name == "__new__"
                        for child in owner.body
                    )
                ):
                    return owner
        if isinstance(definition, ast.alias) and definition in self.imports:
            module, name = self.imports[definition]
            if name is None:
                return module if module in self.trees else None
            target = self.binding_target(
                self.symbols.get(module, {}).get(name), seen, instances=instances
            )
            return (
                target
                if target is not None
                else (f"{module}.{name}" if f"{module}.{name}" in self.trees else None)
            )
        if instances and isinstance(definition, ast.arg) and definition.arg in {"self", "cls"}:
            function = self.parents.get(self.parents.get(definition, definition))
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = self.parents.get(function)
                if (
                    isinstance(owner, ast.ClassDef)
                    and self.descriptor(function) != "staticmethod"
                    and function.args.args
                    and function.args.args[0] is definition
                ):
                    return owner
        return None

    def target(
        self, node: ast.AST, seen: set[int] | None = None, *, instances: bool = True
    ) -> ast.AST | str | None:
        seen = set() if seen is None else seen
        if isinstance(node, ast.Name):
            return self.binding_target(self.bindings.get(node), seen, instances=instances)
        if isinstance(node, ast.Lambda):
            return node
        if isinstance(node, ast.Attribute):
            owner = self.target(node.value, seen, instances=instances)
            if isinstance(owner, str):
                return self.binding_target(
                    self.symbols.get(owner, {}).get(node.attr), seen, instances=instances
                )
            if isinstance(owner, ast.ClassDef):
                binding = next(
                    (binding for binding in self.node_bindings(owner) if binding.name == node.attr),
                    None,
                )
                return self.binding_target(binding, seen, instances=instances)
        elif instances and isinstance(node, ast.Call):
            # Calling an inferred instance invokes __call__, not its class's
            # constructor, and may return an entirely different receiver.
            owner = self.target(node.func, seen, instances=False)
            if isinstance(owner, ast.ClassDef):
                return owner
        return None

    def fresh_name(self) -> str:
        # The caller reserves lexical names and keyword keys for this scope.
        while True:
            number = self.next_name
            self.next_name += 1
            name = ""
            while True:
                name = chr(ord("a") + number % 26) + name
                number = number // 26 - 1
                if number < 0:
                    break
            if name not in self.used:
                self.used.add(name)
                return name

    def exposed_functions(self, module: str, seen: set[str] | None = None) -> set[Function]:
        if seen is None:
            seen = set()
        if module in seen:
            return set()
        seen.add(module)
        exposed = {function for function in self.functions if self.modules[function] == module}
        for binding in self.symbols.get(module, {}).values():
            target = self.binding_target(binding, set())
            if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                exposed.add(target)
            elif isinstance(target, ast.ClassDef):
                exposed.update(
                    child
                    for child in ast.walk(target)
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                )
            elif isinstance(target, str):
                exposed.update(self.exposed_functions(target, seen))
        return exposed

    def analyze_calls(self) -> tuple[dict[Function, list[ast.Call]], set[Function], set[Function]]:
        """Find closed callers and distinguish dynamic keywords from reflection."""
        calls: dict[Function, list[ast.Call]] = {function: [] for function in self.functions}
        blocked: set[Function] = set()
        reflected: set[Function] = set()
        by_name: dict[str, list[Function]] = {}
        methods_by_name: dict[str, list[Function]] = {}
        if any(inspects_frames(tree) for tree in self.trees.values()):
            # Stack/frame APIs can inspect any active scope, not just a known
            # function object. There is no safe cross-module rename in that case.
            reflected.update(self.functions)
        for module, tree in self.trees.items():
            if getattr(tree, "tainted", False):
                exposed = self.exposed_functions(module)
                blocked.update(exposed)
                reflected.update(exposed)
        for module in self.star_imports:
            exposed = self.exposed_functions(module)
            blocked.update(exposed)
            reflected.update(exposed)
        for function in self.functions:
            if isinstance(function, ast.Lambda):
                parent = self.parents.get(function)
                if not (
                    (isinstance(parent, ast.Call) and parent.func is function)
                    or self.alias_assignment(function) is not None
                ):
                    reflected.add(function)
            else:
                by_name.setdefault(function.name, []).append(function)
                owner = self.parents.get(function)
                if isinstance(owner, ast.ClassDef):
                    methods_by_name.setdefault(function.name, []).append(function)
                descriptor = self.descriptor(function)
                if (
                    function.decorator_list
                    and descriptor is None
                    and not self.forwarding_decorators(function)
                ) or function.name in self.strings:
                    blocked.add(function)
                    reflected.add(function)
                if isinstance(owner, ast.ClassDef) and (
                    owner.bases
                    or owner.decorator_list
                    or owner.keywords
                    or (function.name.startswith("__") and function.name != "__init__")
                    or (
                        descriptor != "staticmethod"
                        and (
                            not function.args.args
                            or function.args.args[0].arg not in {"self", "cls"}
                        )
                    )
                    or any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name in {"__new__", "__getattr__", "__getattribute__"}
                        for child in owner.body
                    )
                ):
                    blocked.add(function)
                    reflected.add(function)
        for alias, (_, name) in self.imports.items():
            if name and self.binding_target(self.bindings.get(alias), set()) is None:
                # An unresolved aliased import must not hide a possible caller
                # merely because its local name differs from the definition.
                blocked.update(by_name.get(name, []))
                reflected.update(by_name.get(name, []))
        for tree in self.trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    target = self.target(node.func)
                    if (
                        isinstance(target, ast.ClassDef)
                        and self.target(node.func, instances=False) is target
                    ):
                        # Ordinary constructors forward their keywords to __init__.
                        target = next(
                            (
                                child
                                for child in target.body
                                if isinstance(child, ast.FunctionDef) and child.name == "__init__"
                            ),
                            None,
                        )
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        calls[target].append(node)
                        if any(
                            arg.arg is None and self.literal_keywords(arg) is None
                            for arg in node.keywords
                        ):
                            blocked.add(target)
                elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                    target = self.target(node)
                    parent = self.parents.get(node)
                    direct_call = isinstance(parent, ast.Call) and parent.func is node
                    if self.alias_assignment(node) is not None:
                        # Follow all uses of the destination instead of treating
                        # the assignment itself as an escape.
                        continue
                    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        if not direct_call:
                            blocked.add(target)
                            reflected.add(target)
                    elif isinstance(node, ast.Attribute):
                        # A receiver whose type is unknown might invoke any
                        # same-named implementation. Do not guess from spelling.
                        # Free functions with that name are not methods; their
                        # escaping references/modules are checked separately.
                        blocked.update(methods_by_name.get(node.attr, []))
                        reflected.update(methods_by_name.get(node.attr, []))
                    # An unresolved lexical name does not refer to every
                    # same-spelled function in unrelated scopes. Actual aliases
                    # and callable arguments escape at their original reference.
                    if isinstance(target, ast.ClassDef):
                        use = self.parents.get(parent) if direct_call and parent else parent
                        receiver = parent if direct_call else node
                        # A single-assignment instance remains closed when all
                        # of its later uses are resolved method calls. Those
                        # references are checked separately by this same walk;
                        # passing/storing/reflecting on it still blocks methods.
                        if (
                            direct_call
                            and isinstance(use, (ast.Assign, ast.AnnAssign))
                            and use.value is receiver
                        ):
                            assigned = use.targets if isinstance(use, ast.Assign) else [use.target]
                            if (
                                len(assigned) == 1
                                and isinstance(assigned[0], ast.Name)
                                and self.target(assigned[0]) is target
                            ):
                                continue
                        if not (
                            isinstance(use, ast.Attribute)
                            and use.value is receiver
                            # Reading ordinary instance/class data does not
                            # expose every method signature. Dunder attributes
                            # such as __dict__ and __class__ still do.
                            and not use.attr.startswith("__")
                        ):
                            methods = [
                                child
                                for child in target.body
                                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                            ]
                            blocked.update(methods)
                            reflected.update(methods)
                    elif isinstance(target, str) and not (
                        isinstance(parent, ast.Attribute)
                        and (
                            self.target(parent) is not None
                            or self.imported_attribute(target, parent)
                        )
                    ):
                        # Passing a module to getattr/vars or reading __dict__
                        # exposes signatures without statically visible callers.
                        exposed = self.exposed_functions(target)
                        blocked.update(exposed)
                        reflected.update(exposed)
                elif isinstance(node, ast.Attribute):
                    blocked.update(methods_by_name.get(node.attr, []))
                    reflected.update(methods_by_name.get(node.attr, []))
        return calls, blocked, reflected

    def run(self) -> None:
        self.renamed = 0
        self.skipped.clear()
        calls, blocked, reflected = self.analyze_calls()
        for function in self.functions:
            tree = self.trees[self.modules[function]]
            parameters = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
            variadics = [arg for arg in (function.args.vararg, function.args.kwarg) if arg]
            if getattr(tree, "tainted", False):
                self.skipped["dynamic namespace"] += len(parameters) + len(variadics)
                continue
            # Reuse short names in independent scopes. Reserve every name in
            # this subtree (including closures), and every caller keyword, so
            # neither lexical capture nor accidental **kwargs capture is possible.
            self.used = set(keyword.kwlist) | {"self", "cls"}
            self.next_name = 0
            for node in ast.walk(function):
                self.used.update(binding.name for binding in self.node_bindings(node))
                if isinstance(node, ast.Name):
                    self.used.add(node.id)
                elif isinstance(node, ast.arg):
                    self.used.add(node.arg)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    self.used.update(node.names)
            for call in calls[function]:
                for call_keyword in call.keywords:
                    if call_keyword.arg is not None:
                        self.used.add(call_keyword.arg)
                    else:
                        self.used.update(
                            cast(str, key.value)
                            for key in self.literal_keywords(call_keyword) or []
                        )
            decorated = (
                not isinstance(function, ast.Lambda)
                and bool(function.decorator_list)
                and self.descriptor(function) is None
                and not self.forwarding_decorators(function)
            )
            for argument in parameters + variadics:
                old = argument.arg
                # A matching string alone is not reflection. Dynamic **kwargs
                # and escaped/reflected callables already block the signature.
                reason = None
                owner = self.parents.get(function)
                if old.startswith("__"):
                    reason = "name-mangled/special parameter"
                elif decorated:
                    reason = "unknown decorator"
                elif function in reflected:
                    reason = (
                        "inheritance/decorated class"
                        if isinstance(owner, ast.ClassDef)
                        and (owner.bases or owner.decorator_list or owner.keywords)
                        else "escaping/reflected callable or receiver"
                    )
                elif argument in parameters and function in blocked:
                    reason = "dynamic keywords/unresolved caller"
                elif argument in parameters and not calls[function]:
                    reason = "no resolved calls"
                if reason is not None:
                    self.skipped[reason] += 1
                    continue
                binding = self.bindings.get(argument)
                if binding is None or any(
                    isinstance(ref, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for ref in binding.references
                ):
                    self.skipped["ambiguous lexical binding"] += 1
                    continue
                new = self.fresh_name()
                # NameBinding.rename normally keeps keyword-capable arg names
                # and inserts aliases. We rewrite verified callers instead.
                binding.references.remove(argument)
                binding.rename(new)
                binding.references.append(argument)
                argument.arg = new
                self.renamed += 1
                if argument in parameters:
                    for call in calls[function]:
                        for keyword_arg in call.keywords:
                            if keyword_arg.arg == old:
                                keyword_arg.arg = new
                            elif keyword_arg.arg is None:
                                for key in self.literal_keywords(keyword_arg) or []:
                                    if key.value == old:
                                        key.value = new

    @staticmethod
    def literal_keywords(argument: ast.keyword) -> list[ast.Constant] | None:
        """Only inline, fully known dictionaries can have their keys rewritten."""
        if not isinstance(argument.value, ast.Dict):
            return None
        keys = argument.value.keys
        if all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keys):
            return cast(list[ast.Constant], keys)
        return None


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


class ReleaseCleanup(ast.NodeTransformer):
    """Discard release-only metadata before python-minifier sees the source."""

    def __init__(
        self,
        tree: ast.Module,
        generic_classes: set[str] | None = None,
        *,
        module: str = "__main__",
        protocol_classes: set[tuple[str, str]] | None = None,
    ) -> None:
        self.typing_modules = {"typing", "typing_extensions"}
        self.typing_names = {}
        self.dataclass_modules = {"dataclasses"}
        self.dataclass_names: set[str] = set()
        self.scope: ast.AST = tree
        self.field_names: set[str] = set()
        self.generic_classes = set(generic_classes or ())
        self.module = module
        self.is_package = getattr(tree, "is_package", False)
        self.protocol_classes = set(protocol_classes or ())
        self.generic_classes.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.type_params
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    if name.name in self.typing_modules:
                        self.typing_modules.add(name.asname or name.name)
                    elif name.name == "dataclasses":
                        self.dataclass_modules.add(name.asname or name.name)
            elif isinstance(node, ast.ImportFrom) and node.module in self.typing_modules:
                for name in node.names:
                    self.typing_names[name.asname or name.name] = name.name
            elif isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
                self.dataclass_names.update(
                    name.asname or name.name for name in node.names if name.name == "dataclass"
                )
                self.field_names.update(
                    name.asname or name.name for name in node.names if name.name == "field"
                )
        self.typed_dicts: set[str] = set()
        classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        self.protocol_names = {
            node.name
            for node in classes
            if any(self.typing_name(base) == "Protocol" for base in node.bases)
        }
        self.protocol_classes.update((self.module, name) for name in self.protocol_names)
        imported_protocol_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
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
            # its presence. Do not apply typing or generic rewrites to it.
            node.target = cast(ast.expr, self.visit(node.target))
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
                def visit_Name(self, reference: ast.Name) -> ast.AST:
                    if isinstance(reference.ctx, ast.Load) and reference.id in type_parameters:
                        return ast.copy_location(ast.Name(id="object", ctx=ast.Load()), reference)
                    return reference

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


def prepare_tree(
    source: str,
    filename: str,
    generic_classes: set[str] | None = None,
    *,
    module: str = "__main__",
    protocol_classes: set[tuple[str, str]] | None = None,
) -> ast.Module:
    """Remove release metadata before argument analysis and minification."""
    tree = ast.parse(source, filename=filename)
    tree.__dict__["is_package"] = Path(filename).name == "__init__.py"
    cleanup = ReleaseCleanup(
        tree,
        generic_classes,
        module=module,
        protocol_classes=protocol_classes,
    )
    tree = cleanup.visit(tree)
    # Drop typing imports made unused by annotation/cast removal, including
    # Literal and overload declarations. Preserve postponed annotations when
    # present: surviving field annotations may contain forward references.
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in cleanup.typing_modules for alias in node.names):
                node.names = [
                    alias
                    for alias in node.names
                    if alias.name not in cleanup.typing_modules
                    or (alias.asname or alias.name) in used
                ]
        elif isinstance(node, ast.ImportFrom):
            if node.module in cleanup.typing_modules:
                node.names = [
                    name
                    for name in node.names
                    if (name.asname or name.name) in used or name.name == "*"
                ]

    class RemoveEmptyImports(ReleaseCleanup):
        def visit_Import(self, node: ast.Import) -> ast.AST | None:
            return node if node.names else None

        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
            return node if node.names else None

    tree = RemoveEmptyImports(tree).visit(tree)
    ast.fix_missing_locations(tree)
    return cast(ast.Module, tree)


def minify_tree(tree: ast.Module, filename: str, *, rename_globals: bool = False) -> str:
    """Shorten locals and, after linking, globals without changing runtime APIs."""
    source = ast.unparse(tree)
    preserved: set[str] = set()
    for node in ast.walk(tree):
        # Keep all classes (including exception subclasses), functions and
        # parameters: logs, callbacks and keyword calls depend on these. Closed
        # private methods have already been renamed with their callers.
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            preserved.add(node.name)
        elif isinstance(node, ast.arg):
            preserved.add(node.arg)
        elif isinstance(node, ast.Name) and node.id.startswith("__") and node.id.endswith("__"):
            preserved.add(node.id)
        elif isinstance(node, ast.AnnAssign):
            # Postponed field annotations and quoted forward references are
            # evaluated by dataclasses/dacite later, outside lexical resolution.
            # Keep their dependencies (including import/type aliases) available.
            pending: list[ast.AST] = [node.annotation]
            while pending:
                annotation = pending.pop()
                for reference in ast.walk(annotation):
                    if isinstance(reference, ast.Name):
                        preserved.add(reference.id)
                    elif isinstance(reference, ast.Constant) and isinstance(reference.value, str):
                        with suppress(SyntaxError):
                            pending.append(ast.parse(reference.value, mode="eval"))
    reflected = inspects_frames(tree)
    result = python_minifier.minify(
        source,
        filename=filename,
        rename_locals=not reflected,
        preserve_locals=sorted(preserved),
        rename_globals=rename_globals and not reflected,
        # BuiltinBinding.rename inserts aliases such as ``A = len`` at module
        # scope. Keep direct builtin calls so Nuitka can recognize/optimize them.
        preserve_globals=sorted(preserved | set(dir(builtins))),
        remove_annotations=False,
        remove_literal_statements=False,
        hoist_literals=False,
        convert_posargs_to_args=False,
        remove_object_base=False,
        remove_builtin_exception_brackets=False,
    )
    compile(result, filename, "exec")
    return result + "\n"


def obfuscate(source: str, filename: str) -> str:
    """Obfuscate one standalone module (the CLI analyzes all modules together)."""
    tree = prepare_tree(source, filename)
    PrivateMethodRenamer({"__main__": tree}).run()
    ArgumentRenamer({"__main__": tree}).run()
    return minify_tree(tree, filename)


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def flatten(trees: dict[str, ast.Module]) -> ast.Module:
    """Link application bindings directly into one global scope, like includes."""
    analysis = ArgumentRenamer(trees)
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
                    target, name = analysis.imports[alias]
                    if target == "__future__":
                        continue
                    if target in trees:
                        dependencies[module].append(target)
                        if isinstance(node, ast.Import) and alias.name in trees:
                            dependencies[module].append(alias.name)
                        child = f"{target}.{name}" if name else ""
                        if child in trees:
                            dependencies[module].append(child)
                    else:
                        preferred = alias.asname or (
                            alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                        )
                        library(target, name, preferred)
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
                        replacements[node] = ast.copy_location(
                            ast.Name(id=target[0], ctx=node.ctx), node
                        )

    # Update global declarations and definitions as well as lexical references.
    for symbols in analysis.symbols.values():
        for binding in symbols.values():
            if id(binding) in names and binding.name != names[id(binding)]:
                binding.rename(names[id(binding)])

    class Link(ast.NodeTransformer):
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
    return ast.fix_missing_locations(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Flatten src/ into a release-only app.min.py.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", action="store_true", help="Explain preserved parameter counts")
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if output == source or output in source.parents or source in output.parents:
        parser.error("source and output must be separate, non-overlapping directories")
    if output.exists():
        parser.error("output already exists; use a fresh release staging directory")
    paths: list[Path] = []
    for directory, directories, files in os.walk(source):
        directories[:] = [name for name in directories if name not in {"tests", "__pycache__"}]
        paths.extend(Path(directory) / name for name in files if name.endswith(".py"))
    trees: dict[str, ast.Module] = {}
    originals: dict[Path, str] = {}
    generic_classes: set[str] = set()
    parsed: dict[Path, ast.Module] = {}
    module_names: set[str] = set()
    for path in sorted(paths):
        relative = path.relative_to(source)
        original = path.read_text(encoding="utf-8")
        name = module_name(relative)
        if name in module_names:
            parser.error(f"ambiguous module name: {name}")
        module_names.add(name)
        parsed[relative] = ast.parse(original, filename=str(relative))
        parsed[relative].__dict__["is_package"] = relative.name == "__init__.py"
        generic_classes.update(
            node.name
            for node in ast.walk(parsed[relative])
            if isinstance(node, ast.ClassDef) and node.type_params
        )
        originals[relative] = original
    # Protocol inheritance can cross module boundaries, so discover it until
    # importing a protocol from another module adds no new protocol classes.
    protocol_classes: set[tuple[str, str]] = set()
    while True:
        discovered = {
            (module_name(relative), protocol)
            for relative, tree in parsed.items()
            for protocol in ReleaseCleanup(
                tree,
                generic_classes,
                module=module_name(relative),
                protocol_classes=protocol_classes,
            ).protocol_names
        }
        if discovered <= protocol_classes:
            break
        protocol_classes.update(discovered)
    for relative, original in originals.items():
        trees[module_name(relative)] = prepare_tree(
            original,
            str(relative),
            generic_classes,
            module=module_name(relative),
            protocol_classes=protocol_classes,
        )
    count = len(trees)
    if not count:
        raise SystemExit("No application Python modules found")
    # Analyze the final lexical scope: module-qualified data references no
    # longer look like reflective escapes, and every internal caller is linked.
    linked = flatten(trees)
    linked_trees = {"__main__": linked}
    methods = PrivateMethodRenamer(linked_trees)
    methods.run()
    renamer = ArgumentRenamer(linked_trees)
    renamer.run()
    result = minify_tree(linked, "app.min.py", rename_globals=True)
    output.mkdir(parents=True)
    (output / "app.min.py").write_text(result, encoding="utf-8")
    before = sum(len(original.encode("utf-8")) for original in originals.values())
    after = len(result.encode("utf-8"))
    print(
        f"Flattened {count} application modules into app.min.py: {before:,} -> {after:,} source bytes"
    )
    print(f"Renamed {renamer.renamed:,}/{renamer.renamed + renamer.skipped.total():,} parameters")
    print(f"Renamed {methods.renamed:,} private methods")
    if args.report:
        for reason, preserved in renamer.skipped.most_common():
            print(f"  Preserved {preserved:,}: {reason}")


if __name__ == "__main__":
    main()

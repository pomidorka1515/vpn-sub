from __future__ import annotations

import ast
import keyword
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from python_minifier.ast_annotation import add_parent
from python_minifier.rename.bind_names import bind_names
from python_minifier.rename.binding import BuiltinBinding
from python_minifier.rename.mapper import add_namespace
from python_minifier.rename.resolve_names import resolve_names

from ..analysis import Binding, inspects_frames

if TYPE_CHECKING:
    from ..analysis import Function

__all__: list[str] = ["ArgumentRenamer"]


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

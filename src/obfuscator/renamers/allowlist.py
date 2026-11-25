from __future__ import annotations

import ast
import json
import keyword
import sys
from collections import deque
from contextlib import suppress
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from ..analysis import inspects_frames
from .attribute_flow import AttributeFlow
from .attributes import plan_attributes
from .base import ArgumentRenamer
from .external import ExternalMembers

if TYPE_CHECKING:
    from ..analysis import Binding

__all__: list[str] = ["AllowlistRenamer", "read_allowlist"]


def read_allowlist(value: str) -> list[str]:
    """Read a JSON array or a file containing one; an empty file opts in nothing."""
    try:
        text = (
            value
            if value.lstrip().startswith(("[", "{"))
            else Path(value).read_text(encoding="utf-8")
        )
        decoded: object = json.loads(text) if text.strip() else []
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read allowlist: {error}") from error
    if not isinstance(decoded, list):
        raise ValueError("Allowlist must be a JSON list of strings")
    result: list[str] = []
    for entry in cast(list[object], decoded):
        if not isinstance(entry, str):
            raise ValueError("Allowlist must be a JSON list of strings")
        parts = entry.removesuffix("*").split(":") if entry.startswith("f:") else entry.split(":")
        lexical_parts = parts[1:]
        if lexical_parts and lexical_parts[0].endswith(".py"):
            source_parts = lexical_parts[0][:-3].split("/")
            if any(not part.isidentifier() or keyword.iskeyword(part) for part in source_parts):
                raise ValueError(f"Invalid allowlist source path in {entry!r}")
            lexical_parts = lexical_parts[1:]
        if (
            parts[0] not in {"c", "f", "a", "v", "attr"}
            or len(lexical_parts) < (2 if parts[0] in {"a", "attr"} else 1)
            or any(not part.isidentifier() or keyword.iskeyword(part) for part in lexical_parts)
        ):
            raise ValueError(
                f"Invalid allowlist entry {entry!r}; expected c:path, f:path, f:path*, a:path:arg, attr:class:name or v:path"
            )
        if entry in result:
            raise ValueError(f"Duplicate allowlist entry {entry!r}")
        result.append(entry)
    return result


class AllowlistRenamer(ArgumentRenamer):
    """Opt-in binding renames, with C3 inheritance and fail-closed static references.

    Attribute selections also rewrite verified literal builtin reflection calls.
    Unknown receivers, escaping parameter APIs and dynamic keyword dictionaries
    are errors, rather than guesses based on attribute spelling.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.linked_classes: dict[Binding, ast.ClassDef] = {}
        super().__init__({"__main__": tree})
        self.tree = tree
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                node.__dict__.setdefault("original_name", node.name)
            elif isinstance(node, ast.Name):
                node.__dict__.setdefault("original_name", node.id)
            elif isinstance(node, ast.arg):
                node.__dict__.setdefault("original_name", node.arg)
        self.classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        self.own_members: dict[ast.ClassDef, dict[str, Binding]] = {
            owner: {binding.name: binding for binding in reversed(self.node_bindings(owner))}
            for owner in self.classes
        }
        self.inherited_members: dict[ast.ClassDef, dict[str, Binding]] = {}
        self.receiver_families: dict[ast.ClassDef, frozenset[ast.ClassDef]] = {}
        self.callable_index: dict[ast.expr, set[ast.AST]] = {}
        self.class_alias_targets: set[ast.Name] = {
            target
            for node in self.classes
            if isinstance(target := node.__dict__.get("class_alias_target"), ast.Name)
        }
        self.mros: dict[ast.ClassDef, list[ast.ClassDef]] = {}
        self.resolving: set[ast.ClassDef] = set()
        self.external_members = ExternalMembers()
        self.linked_classes = {
            self.bindings[target]: owner
            for owner in self.classes
            if isinstance(target := owner.__dict__.get("class_alias_target"), ast.Name)
            and self.definitions(self.bindings[target]) == [target]
        }

    def binding_target(
        self, binding: Binding | None, seen: set[int], *, instances: bool = True
    ) -> ast.AST | str | None:
        if binding is not None and id(binding) not in seen:
            owner = self.linked_classes.get(binding)
            if isinstance(owner, ast.ClassDef):
                return owner
        return super().binding_target(binding, seen, instances=instances)

    @cached_property
    def attribute_flow(self) -> AttributeFlow:
        return AttributeFlow(self)

    def external_base(
        self, node: ast.expr, seen: set[ast.AST] | None = None, *, suffix: tuple[str, ...] = (),
    ) -> type:
        seen = set() if seen is None else seen
        if node in seen:
            raise ValueError("cyclic external base alias")
        seen.add(node)
        if isinstance(node, ast.Subscript):
            node = node.value
        path: list[str] = []
        while isinstance(node, ast.Attribute):
            path.insert(0, node.attr)
            node = node.value
        path.extend(suffix)
        binding = self.bindings.get(node)
        if isinstance(cast(object, binding), BuiltinBinding) and isinstance(node, ast.Name):
            return self.external_members.resolve("builtins", [node.id, *path])
        definitions = self.definitions(binding) if binding is not None else []
        if len(definitions) == 1:
            definition = definitions[0]
            if isinstance(definition, ast.alias):
                module, name = self.imports[definition]
                return self.external_members.resolve(module, [*([name] if name else []), *path])
            if isinstance(definition, ast.Name):
                assignment = self.parents.get(definition)
                if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and assignment.value is not None:
                    return self.external_base(assignment.value, seen, suffix=tuple(path))
        raise ValueError(f"unresolved base {ast.unparse(node)!r}")

    def check_receiver(self, owner: ast.ClassDef, name: str | None = None) -> None:
        for base in self.mro(owner):
            if (
                base.keywords
                or not self.safe_class_decorators(base)
                or any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name in {"__getattr__", "__getattribute__", "__new__"}
                    for child in base.body
                )
            ):
                raise ValueError(f"Unsupported dynamic receiver class {base.name!r}")
            for parent in base.bases:
                if isinstance(super().target(parent, instances=False), ast.ClassDef):
                    continue
                try:
                    members = self.external_members.members(self.external_base(parent))
                except (ImportError, AttributeError, OSError, TypeError, ValueError, SyntaxError) as error:
                    raise ValueError(
                        f"Cannot verify external inheritance for receiver {base.name!r}: {error}"
                    ) from error
                if name is not None and name in members:
                    raise ValueError(f"External inherited member conflict for {base.name!r}: {name!r}")

    def path(self, node: ast.AST, *, original: bool = True) -> tuple[str, ...]:
        names: list[str] = []
        while not isinstance(node, ast.Module):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(
                    cast(str, node.__dict__.get("original_name", node.name))
                    if original
                    else node.name
                )
            node = self.parents[node]
        return tuple(reversed(names))

    def mro(self, owner: ast.ClassDef) -> list[ast.ClassDef]:
        if owner in self.mros:
            return self.mros[owner]
        if owner in self.resolving:
            raise ValueError(f"Cyclic inheritance at {owner.name}")
        self.resolving.add(owner)
        bases = [
            super(AllowlistRenamer, self).target(base, instances=False) for base in owner.bases
        ]
        # External bases have no application members we can rewrite. References
        # which cannot be resolved to an internal member are rejected below.
        internal = [base for base in bases if isinstance(base, ast.ClassDef)]
        sequences = [self.mro(base).copy() for base in internal] + [internal.copy()]
        result = [owner]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            head = next(
                (
                    seq[0]
                    for seq in sequences
                    if all(seq[0] not in other[1:] for other in sequences)
                ),
                None,
            )
            if head is None:
                raise ValueError(f"Inconsistent inheritance at {owner.name}")
            result.append(head)
            for sequence in sequences:
                if sequence[0] is head:
                    sequence.pop(0)
        self.resolving.remove(owner)
        self.mros[owner] = result
        return result

    def member(self, owner: ast.ClassDef, name: str) -> Binding | None:
        members: dict[str, Binding] | None = self.inherited_members.get(owner)
        if members is None:
            members = {}
            for base in reversed(self.mro(owner)):
                members.update(self.own_members[base])
            self.inherited_members[owner] = members
        return members.get(name)

    def receiver_family(self, owner: ast.ClassDef) -> frozenset[ast.ClassDef]:
        family = self.receiver_families.get(owner)
        if family is None:
            family = frozenset(cls for cls in self.classes if owner in self.mro(cls))
            self.receiver_families[owner] = family
        return family

    def receiver(self, node: ast.expr, seen: set[int] | None = None) -> tuple[set[ast.ClassDef], bool]:
        """Return possible classes and whether this is a polymorphic self/cls."""
        owner: ast.AST | str | None
        if isinstance(node, ast.Name):
            binding = self.bindings.get(node)
            definitions = self.definitions(binding) if binding else []
            if len(definitions) == 1 and isinstance(definitions[0], ast.arg):
                argument = definitions[0]
                function = self.parents[self.parents[argument]]
                owner = self.parents.get(function)
                if (
                    isinstance(owner, ast.ClassDef)
                    and isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and (positional := function.args.posonlyargs + function.args.args)
                    and positional[0] is argument
                    and self.descriptor(function) != "staticmethod"
                ):
                    return set(self.receiver_family(owner)), True
            if len(definitions) == 1 and isinstance(definitions[0], ast.Name):
                assignment = self.parents.get(definitions[0])
                if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and isinstance(
                    assignment.value, ast.Call
                ):
                    owner = super().target(assignment.value.func, seen, instances=False)
                    if isinstance(owner, ast.ClassDef):
                        if any(
                            isinstance(child, ast.FunctionDef) and child.name == "__new__"
                            for base in self.mro(owner)
                            for child in base.body
                        ):
                            return set(), False
                        return {owner}, False
        if isinstance(node, ast.Call):
            if (
                len(node.args) == 1 and not node.keywords
                and isinstance(cast(object, self.bindings.get(node.func)), BuiltinBinding)
                and isinstance(node.func, ast.Name) and node.func.id == "type"
            ):
                owners, _ = self.receiver(node.args[0], seen)
                return {cls for owner in owners for cls in self.receiver_family(owner)}, True
            owner = super().target(node.func, seen, instances=False)
            if isinstance(owner, ast.ClassDef):
                if any(
                    isinstance(child, ast.FunctionDef) and child.name == "__new__"
                    for base in self.mro(owner)
                    for child in base.body
                ):
                    return set(), False
                return {owner}, False
        owners = super().type_targets(node, seen)
        if owners:
            return owners, False
        owner = super().target(node, seen, instances=False)
        return ({owner}, False) if isinstance(owner, ast.ClassDef) else (set(), False)

    def super_bindings(self, node: ast.Attribute) -> set[Binding] | None:
        """Resolve zero-argument builtin super against visible receiver MROs."""
        call = node.value
        if not isinstance(call, ast.Call) or call.args or call.keywords:
            return None
        builtin = self.bindings.get(call.func)
        if not isinstance(cast(object, builtin), BuiltinBinding) or getattr(builtin, "name", "") != "super":
            return None
        function: ast.AST | None = self.parents.get(call)
        while function is not None and not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            function = self.parents.get(function)
        owner = self.parents.get(function) if function is not None else None
        if not isinstance(owner, ast.ClassDef) or not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        if self.descriptor(function) == "staticmethod" or not (function.args.posonlyargs or function.args.args):
            return None
        bindings: set[Binding] = set()
        external: bool = False
        for receiver in self.classes:
            order = self.mro(receiver)
            if owner not in order:
                continue
            binding = next((
                binding for base in order[order.index(owner) + 1:]
                if (binding := self.own_members[base].get(node.attr)) is not None
            ), None)
            if binding is None:
                external = True
            else:
                bindings.add(binding)
        if external and bindings:
            raise ValueError(f"Ambiguous super attribute {ast.unparse(node)!r}")
        return bindings

    def attribute_bindings(self, node: ast.Attribute, seen: set[int] | None = None) -> set[Binding]:
        inherited = self.super_bindings(node)
        if inherited is not None:
            return inherited
        owners, _ = self.receiver(node.value, seen)
        return {
            binding for owner in owners if (binding := self.member(owner, node.attr)) is not None
        }

    def target(
        self, node: ast.AST, seen: set[int] | None = None, *, instances: bool = True
    ) -> ast.AST | str | None:
        if isinstance(node, ast.Attribute):
            bindings = self.attribute_bindings(node, seen)
            if len(bindings) == 1:
                return self.binding_target(next(iter(bindings)), seen or set(), instances=instances)
            return None
        return super().target(node, seen, instances=instances)

    def callable_targets(self, node: ast.expr) -> set[ast.AST]:
        targets = self.callable_index.get(node)
        if targets is None:
            targets = self.resolve_callable_targets(node)
            self.callable_index[node] = targets
        return targets

    def resolve_callable_targets(self, node: ast.expr) -> set[ast.AST]:
        if isinstance(node, ast.Attribute):
            return {
                definition
                for binding in self.attribute_bindings(node)
                for definition in self.definitions(binding)
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        target = super().target(node, instances=False)
        if isinstance(target, ast.ClassDef):
            binding = self.member(target, "__init__")
            return set(self.definitions(binding)) if binding else set()
        return (
            {target}
            if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            else set()
        )

    def quoted_annotations(self, node: ast.AST) -> list[ast.Constant]:
        """Find forward-reference strings, excluding typing literal values/metadata."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node]
        if isinstance(node, ast.Subscript):
            reference: ast.expr = (
                node.value.value if isinstance(node.value, ast.Attribute) else node.value
            )
            binding: Binding | None = self.bindings.get(reference)
            definitions: list[ast.AST] = self.definitions(binding) if binding is not None else []
            if len(definitions) == 1 and isinstance(definitions[0], ast.alias):
                module: str
                name: str | None
                module, name = self.imports.get(definitions[0], ("", None))
                if isinstance(node.value, ast.Attribute):
                    name = node.value.attr if name is None else None
                if module in {"typing", "typing_extensions"}:
                    if name == "Literal":
                        return []
                    if name == "Annotated" and isinstance(node.slice, ast.Tuple):
                        return self.quoted_annotations(node.slice.elts[0])
        return [
            reference
            for child in ast.iter_child_nodes(node)
            for reference in self.quoted_annotations(child)
        ]

    def apply(self, entries: list[str]) -> dict[str, str]:
        if not entries:
            return {}
        attribute_entries = [entry for entry in entries if entry.startswith("attr:")]
        entries = [entry for entry in entries if not entry.startswith("attr:")]
        attribute_plan, attribute_bindings, attribute_renames = plan_attributes(
            self, attribute_entries
        )
        candidates: dict[tuple[str, ...], list[tuple[str, Binding, ast.AST]]] = {}
        for scope in ast.walk(self.tree):
            for binding in self.node_bindings(scope):
                if isinstance(cast(object, binding), BuiltinBinding):
                    continue
                definitions = self.definitions(binding)
                declarations = [
                    ref
                    for ref in definitions
                    if isinstance(ref, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                arguments = [ref for ref in definitions if isinstance(ref, ast.arg)]
                stores = [
                    ref
                    for ref in definitions
                    if isinstance(ref, ast.Name)
                    and isinstance(ref.ctx, ast.Store)
                    and ref not in self.class_alias_targets
                ]
                if declarations:
                    for declaration in declarations:
                        candidates.setdefault(self.path(declaration), []).append(
                            (
                                "c" if isinstance(declaration, ast.ClassDef) else "f",
                                binding,
                                declaration,
                            )
                        )
                elif arguments:
                    argument = arguments[0]
                    name = cast(str, argument.__dict__.get("original_name", argument.arg))
                    candidates.setdefault((*self.path(scope), name), []).append(
                        ("a", binding, argument)
                    )
                elif stores and all(
                    isinstance(ref, (ast.Name, ast.Global, ast.Nonlocal)) for ref in definitions
                ):
                    name = cast(str, stores[0].__dict__.get("original_name", stores[0].id))
                    candidates.setdefault((*self.path(scope), name), []).append(
                        ("v", binding, scope)
                    )
        # Register qualified paths alongside legacy lexical paths. The file is
        # provenance, never a generated alias or an extra lexical scope.
        for path, candidate_targets in list(candidates.items()):
            for actual, binding, definition in candidate_targets:
                origin = definition
                if actual == "v":
                    origin = next(
                        ref
                        for ref in self.definitions(binding)
                        if isinstance(ref, ast.Name) and isinstance(ref.ctx, ast.Store)
                    )
                source_file = origin.__dict__.get("original_file")
                if isinstance(source_file, str):
                    qualified_binding = binding
                    if actual == "c" and isinstance(
                        qualified_target := definition.__dict__.get("class_alias_target"), ast.Name
                    ):
                        qualified_binding = self.bindings[qualified_target]
                    candidates.setdefault((source_file, *path), []).append(
                        (actual, qualified_binding, definition)
                    )
        selected: dict[Binding, tuple[str, str, ast.AST]] = {}
        pending = deque(dict.fromkeys(entries))
        queued = set(entries)
        while pending:
            entry = pending.popleft()
            expand_parameters = entry.startswith("f:") and entry.endswith("*")
            kind, *parts = (entry[:-1] if expand_parameters else entry).split(":")
            matches = candidates.get(tuple(parts), [])
            if len(matches) != 1:
                message: str = (
                    "ambiguous target"
                    if matches
                    else "target not found (or unsupported binding type)"
                )
                if not matches:
                    suggestions: list[str] = sorted(
                        f"{kind}:{':'.join(path)}{'*' if expand_parameters else ''}"
                        for path, targets in candidates.items()
                        if len(path) > len(parts)
                        and (
                            path[0] == parts[0]
                            and path[-len(parts[1:]) :] == tuple(parts[1:])
                            if parts[0].endswith(".py")
                            else not path[0].endswith(".py")
                            and path[-len(parts) :] == tuple(parts)
                        )
                        and any(actual == kind for actual, _, _ in targets)
                    )
                    if suggestions:
                        message += "; use the full lexical path: " + ", ".join(
                            repr(suggestion) for suggestion in suggestions
                        )
                raise ValueError(f"Allowlist {entry!r}: {message}")
            actual, binding, definition = matches[0]
            if kind != actual:
                raise ValueError(f"Allowlist {entry!r}: incorrect type; target requires {actual!r}")
            if definition.__dict__.get("no_rename") or (
                expand_parameters
                and isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    argument.__dict__.get("no_rename")
                    for argument in definition.args.posonlyargs
                    + definition.args.args
                    + definition.args.kwonlyargs
                )
            ):
                print(
                    f"Warning: skipping allowlist {entry!r}: overlaps with a no_rename"
                    " or no_rename_arg decorator; rule ignored",
                    file=sys.stderr,
                )
                continue
            definitions = self.definitions(binding)
            alias_references: list[ast.Name] = cast(
                list[ast.Name], definition.__dict__.get("class_alias_references", [])
            )
            if kind == "c":
                definitions = [ref for ref in definitions if ref not in alias_references]
                alias_target: ast.Name | None = cast(
                    ast.Name | None, definition.__dict__.get("class_alias_target")
                )
                if alias_target is not None:
                    # Rebinding the real linked alias is still ambiguous, even
                    # though the temporary class slot itself has one definition.
                    alias_binding = self.bindings[alias_target]
                    if binding is alias_binding:
                        definitions = [definition]
                    definitions.extend(
                        ref
                        for ref in self.definitions(alias_binding)
                        if ref is not alias_target
                    )
            if kind in {"c", "f"} and len(definitions) != 1:
                description = "class" if kind == "c" else "function"
                raise ValueError(f"Allowlist {entry!r}: ambiguous {description} binding")
            if expand_parameters:
                if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    raise ValueError(f"Allowlist {entry!r}: expected a function or method")
                expanded_parameters = (
                    definition.args.posonlyargs + definition.args.args + definition.args.kwonlyargs
                )
                for parameter in expanded_parameters:
                    name = cast(str, parameter.__dict__.get("original_name", parameter.arg))
                    parameter_entry = f"a:{':'.join(parts)}:{name}"
                    if parameter_entry not in queued:
                        queued.add(parameter_entry)
                        pending.append(parameter_entry)
            if kind in {"c", "f"}:
                scope = self.parents[definition]
            elif kind == "a":
                scope = self.parents[self.parents[definition]]
            else:
                scope = definition
            current_path = (*self.path(scope, original=False), binding.name)
            # Candidates are keyed by original lexical paths. A renamed parent
            # does not mean this binding was renamed; only skip the latter.
            linked_collision = parts[0].endswith(".py") and any(
                ref.__dict__.get("linked_name") == binding.name
                for ref in [*definitions, *self.definitions(binding)]
            )
            if binding.name != parts[-1] and not linked_collision:
                print(
                    f"Warning: skipping allowlist {entry!r}: already renamed by an earlier "
                    f"transformation to {kind}:{':'.join(current_path)}",
                    file=sys.stderr,
                )
                continue
            if binding.name.startswith("__"):
                raise ValueError(f"Allowlist {entry!r}: special/name-mangled names are unsupported")
            if isinstance(definition, ast.arg):
                function = self.parents[self.parents[definition]]
                if not isinstance(
                    function, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) or definition in {function.args.vararg, function.args.kwarg}:
                    raise ValueError(
                        f"Allowlist {entry!r}: *args/**kwargs bindings cannot be selected"
                    )
                function_binding = self.bindings.get(function)
                if function_binding is None or self.definitions(function_binding) != [function]:
                    raise ValueError(f"Allowlist {entry!r}: ambiguous/reassigned callable")
                if (
                    function.decorator_list
                    and self.descriptor(function) is None
                    and not self.forwarding_decorators(function)
                ):
                    raise ValueError(
                        f"Allowlist {entry!r}: parameter belongs to an unknown decorator API"
                    )
            if (
                kind == "v"
                and isinstance(scope := definition, ast.ClassDef)
                and any(
                    isinstance(child, ast.AnnAssign)
                    and isinstance(child.target, ast.Name)
                    and child.target.id == binding.name
                    for child in scope.body
                )
            ):
                raise ValueError(f"Allowlist {entry!r}: runtime field renaming is unsupported")
            if kind == "f" and isinstance(self.parents.get(definition), ast.ClassDef):
                self.check_receiver(cast(ast.ClassDef, self.parents[definition]), binding.name)
            elif kind == "a":
                function = self.parents[self.parents[definition]]
                owner = self.parents.get(function)
                if isinstance(owner, ast.ClassDef) and isinstance(
                    function, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    self.check_receiver(owner, function.name)
            selected[binding] = (kind, entry, definition)
        if selected.keys() & attribute_bindings.keys():
            raise ValueError("Conflicting variable and attribute allowlist selections")
        self.used.update(self.strings)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                self.used.add(node.id)
            elif isinstance(node, (ast.arg, ast.keyword)) and node.arg is not None:
                self.used.add(node.arg)
            elif isinstance(node, ast.Attribute):
                self.used.add(node.attr)
            self.used.update(binding.name for binding in self.node_bindings(node))
        renamed = {binding: self.fresh_name() for binding in selected}
        attributes: dict[ast.Attribute, str] = {}
        keywords: dict[ast.keyword | ast.Constant, str] = {}
        member_names = {
            binding.name
            for binding, (kind, _, definition) in selected.items()
            if (kind in {"c", "f"} and isinstance(self.parents.get(definition), ast.ClassDef))
            or (kind == "v" and isinstance(definition, ast.ClassDef))
        }
        parameter_functions: dict[ast.AST, dict[str, Binding]] = {}
        parameter_methods: set[Binding] = set()
        watched = set(selected)
        for binding, (kind, _, definition) in selected.items():
            if kind == "a":
                function = self.parents[self.parents[definition]]
                parameter_functions.setdefault(function, {})[binding.name] = binding
                if isinstance(self.parents.get(function), ast.ClassDef):
                    member_binding = self.bindings.get(function)
                    if member_binding is not None:
                        watched.add(member_binding)
                        parameter_methods.add(member_binding)
                        member_names.add(member_binding.name)
        constructors = {
            function for function in parameter_functions
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and function.name == "__init__"
        }
        if constructors:
            if (
                inspects_frames(self.tree)
                or getattr(self.tree, "tainted", False)
                or "__init__" in self.strings
            ):
                raise ValueError("Escaping/reflected constructor with allowlisted parameters")
            for node in ast.walk(self.tree):
                reflected_receiver: ast.expr
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "vars", "dir"}
                    and node.args
                ):
                    reflected_receiver = node.args[0]
                elif isinstance(node, ast.Attribute) and node.attr in {
                    "__class__", "__dict__", "__getattribute__"
                }:
                    reflected_receiver = node.value
                else:
                    continue
                owners, _ = self.receiver(reflected_receiver)
                if any(
                    (constructor_binding := self.member(owner, "__init__")) is not None
                    and constructors.intersection(self.definitions(constructor_binding))
                    for owner in owners
                ):
                    raise ValueError("Escaping/reflected constructor with allowlisted parameters")
        member_families: dict[str, set[ast.ClassDef]] = {}
        selected_names = {binding.name for binding in selected}
        # Resolve everything against the original tree before mutating any name.
        for node in ast.walk(self.tree):
            if id(node) in self.annotation_nodes:
                continue
            if isinstance(node, ast.Attribute) and node.attr in member_names:
                targets = self.attribute_bindings(node)
                flow_owners: set[ast.ClassDef] | None = None
                if not targets and self.super_bindings(node) is None:
                    # Protocols are stripped from the staging tree. Recover
                    # concrete receivers from visible callers instead of
                    # assuming every same-spelled member uses the selected slot.
                    flow = self.attribute_flow
                    receiver = flow.value(node.value)
                    flow_owners = set(receiver.owners)
                    if not flow_owners:
                        if node.attr not in member_families:
                            member_families[node.attr] = {
                                owner for owner in self.classes if self.member(owner, node.attr) in watched
                            }
                        if flow.unrelated(node.value, member_families[node.attr]):
                            continue
                    members = {self.member(owner, node.attr) for owner in flow_owners}
                    if receiver.unknown or not members or None in members:
                        raise ValueError(f"Unresolved receiver for allowlisted attribute {flow.describe(node)}")
                    targets = {member for member in members if member is not None}
                affected = targets & watched
                if affected:
                    if len(targets) != 1:
                        raise ValueError(f"Ambiguous polymorphic attribute {ast.unparse(node)!r}")
                    owners = self.receiver(node.value)[0] if flow_owners is None else flow_owners
                    for owner in owners:
                        self.check_receiver(owner, node.attr)
                    member_binding = next(iter(affected))
                    if flow_owners is not None and member_binding in parameter_methods:
                        raise ValueError(
                            f"Unresolved receiver for allowlisted parameters of {self.attribute_flow.describe(node)}"
                        )
                    if member_binding in parameter_methods and not isinstance(node.ctx, ast.Load):
                        raise ValueError(
                            f"Reassigned method with allowlisted parameters: {ast.unparse(node)!r}"
                        )
                    if member_binding in renamed:
                        attributes[node] = renamed[member_binding]
            if isinstance(node, ast.AnnAssign):
                for reference in self.quoted_annotations(node.annotation):
                    if isinstance(reference.value, str):
                        with suppress(SyntaxError):
                            quoted = ast.parse(reference.value, mode="eval")
                            names = {
                                ref.id for ref in ast.walk(quoted) if isinstance(ref, ast.Name)
                            }
                            if not names.isdisjoint(selected_names):
                                annotation_entries: list[str] = sorted(
                                    entry
                                    for binding, (_, entry, _) in selected.items()
                                    if binding.name in names
                                )
                                raise ValueError(
                                    "Allowlisted name occurs in a quoted runtime annotation: "
                                    f"{', '.join(annotation_entries)} in {reference.value!r}"
                                )
            if parameter_functions and isinstance(node, ast.Call):
                call_targets = self.callable_targets(node.func)
                affected_functions = call_targets & parameter_functions.keys()
                if affected_functions:
                    if len(call_targets) != 1:
                        raise ValueError(f"Ambiguous keyword caller {ast.unparse(node.func)!r}")
                    parameters = parameter_functions[next(iter(affected_functions))]
                    constructor = super().target(node.func, instances=False)
                    if isinstance(constructor, ast.ClassDef):
                        self.check_receiver(constructor)
                    for call_keyword in node.keywords:
                        if call_keyword.arg is None:
                            keys = self.literal_keywords(call_keyword)
                            if keys is None:
                                raise ValueError(
                                    f"Dynamic **kwargs for allowlisted parameters of {ast.unparse(node.func)!r}"
                                )
                            for key in keys:
                                if key.value in parameters:
                                    keywords[key] = renamed[parameters[key.value]]
                        elif call_keyword.arg in parameters:
                            keywords[call_keyword] = renamed[parameters[call_keyword.arg]]
            if (
                parameter_functions
                and isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(node.ctx, ast.Load)
            ):
                callable_targets = self.callable_targets(node)
                if callable_targets & parameter_functions.keys():
                    parent = self.parents.get(node)
                    if (
                        not (isinstance(parent, ast.Call) and parent.func is node)
                        and self.alias_assignment(node) is None
                    ):
                        raise ValueError(
                            f"Escaping callable with allowlisted parameters: {ast.unparse(node)!r}"
                        )
        removed_statements: set[ast.AST] = set()
        for binding, (_, _, definition) in selected.items():
            if isinstance(definition, ast.arg):
                binding.references.remove(definition)
                binding.rename(renamed[binding])
                binding.references.append(definition)
                definition.arg = renamed[binding]
            elif isinstance(definition, ast.ClassDef) and (
                alias_references := cast(
                    list[ast.Name], definition.__dict__.get("class_alias_references", [])
                )
            ):
                # This class uses a temporary name only until its linked alias
                # is assigned. Other uses of that slot (e.g. a builtin after
                # the cleanup del) must not follow the class rename.
                definition.name = renamed[binding]
                alias_target = cast(ast.Name, definition.__dict__["class_alias_target"])
                if self.bindings[alias_target] is binding:
                    binding.rename(renamed[binding])
                    # The qualified class now owns its final global name;
                    # retaining the old temporary-slot cleanup would delete it.
                    removed_statements.add(self.parents[alias_target])
                    removed_statements.update(
                        self.parents[ref] for ref in alias_references
                        if isinstance(ref.ctx, ast.Del)
                    )
                for alias_reference in alias_references:
                    alias_reference.id = renamed[binding]
            else:
                binding.rename(renamed[binding])
        if removed_statements:
            class RemoveClassSlots(ast.NodeTransformer):
                def visit_Assign(self, node: ast.Assign) -> ast.AST | None:
                    return None if node in removed_statements else self.generic_visit(node)

                def visit_Delete(self, node: ast.Delete) -> ast.AST | None:
                    return None if node in removed_statements else self.generic_visit(node)

            RemoveClassSlots().visit(self.tree)
        for node, name in attributes.items():
            node.attr = name
        for node, name in keywords.items():
            if isinstance(node, ast.keyword):
                node.arg = name
            else:
                node.value = name
        for binding, name in attribute_bindings.items():
            binding.rename(name)
        for node, name in attribute_plan.items():
            if isinstance(node, ast.Attribute):
                node.attr = name
            else:
                node.value = name
        return {
            **{entry: renamed[binding] for binding, (_, entry, _) in selected.items()},
            **attribute_renames,
        }

from __future__ import annotations

import ast
import json
import keyword
import sys
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from python_minifier.rename.binding import BuiltinBinding

from .base import ArgumentRenamer

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
        if (
            parts[0] not in {"c", "f", "a", "v"}
            or len(parts) < (3 if parts[0] == "a" else 2)
            or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts[1:])
        ):
            raise ValueError(
                f"Invalid allowlist entry {entry!r}; expected c:path, f:path, f:path*, a:path:arg or v:path"
            )
        if entry in result:
            raise ValueError(f"Duplicate allowlist entry {entry!r}")
        result.append(entry)
    return result


class AllowlistRenamer(ArgumentRenamer):
    """Opt-in binding renames, with C3 inheritance and fail-closed static references.

    Strings and reflective/external APIs are deliberately not rewritten. Unknown
    receivers, escaping parameter APIs and dynamic keyword dictionaries are errors,
    rather than guesses based on attribute spelling.
    """

    def __init__(self, tree: ast.Module) -> None:
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
        self.mros: dict[ast.ClassDef, list[ast.ClassDef]] = {}
        self.resolving: set[ast.ClassDef] = set()

    def check_receiver(self, owner: ast.ClassDef) -> None:
        for base in self.mro(owner):
            if (
                base.keywords
                or base.decorator_list
                or any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name in {"__getattr__", "__getattribute__", "__new__"}
                    for child in base.body
                )
            ):
                raise ValueError(f"Unsupported dynamic receiver class {base.name!r}")
            if any(
                not isinstance(
                    super(AllowlistRenamer, self).target(parent, instances=False), ast.ClassDef
                )
                and not (isinstance(parent, ast.Name) and parent.id == "object")
                for parent in base.bases
            ):
                raise ValueError(f"Cannot verify external inheritance for receiver {base.name!r}")

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
        for base in self.mro(owner):
            binding = next((item for item in self.node_bindings(base) if item.name == name), None)
            if binding is not None:
                return binding
        return None

    def receiver(self, node: ast.expr) -> tuple[set[ast.ClassDef], bool]:
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
                    and function.args.args
                    and function.args.args[0] is argument
                    and self.descriptor(function) != "staticmethod"
                ):
                    return {cls for cls in self.classes if owner in self.mro(cls)}, True
            if len(definitions) == 1 and isinstance(definitions[0], ast.Name):
                assignment = self.parents.get(definitions[0])
                if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and isinstance(
                    assignment.value, ast.Call
                ):
                    owner = super().target(assignment.value.func, instances=False)
                    if isinstance(owner, ast.ClassDef):
                        if any(
                            isinstance(child, ast.FunctionDef) and child.name == "__new__"
                            for base in self.mro(owner)
                            for child in base.body
                        ):
                            return set(), False
                        return {owner}, False
        if isinstance(node, ast.Call):
            owner = super().target(node.func, instances=False)
            if isinstance(owner, ast.ClassDef):
                if any(
                    isinstance(child, ast.FunctionDef) and child.name == "__new__"
                    for base in self.mro(owner)
                    for child in base.body
                ):
                    return set(), False
                return {owner}, False
        owner = super().target(node, instances=False)
        return ({owner}, False) if isinstance(owner, ast.ClassDef) else (set(), False)

    def attribute_bindings(self, node: ast.Attribute) -> set[Binding]:
        owners, _ = self.receiver(node.value)
        return {
            binding for owner in owners if (binding := self.member(owner, node.attr)) is not None
        }

    def target(
        self, node: ast.AST, seen: set[int] | None = None, *, instances: bool = True
    ) -> ast.AST | str | None:
        if isinstance(node, ast.Attribute):
            bindings = self.attribute_bindings(node)
            if len(bindings) == 1:
                return self.binding_target(next(iter(bindings)), seen or set(), instances=instances)
            return None
        return super().target(node, seen, instances=instances)

    def callable_targets(self, node: ast.expr) -> set[ast.AST]:
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
                    if isinstance(ref, ast.Name) and isinstance(ref.ctx, ast.Store)
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
                        and path[-len(parts) :] == tuple(parts)
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
            if kind in {"c", "f"} and len(self.definitions(binding)) != 1:
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
            if binding.name != parts[-1]:
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
            selected[binding] = (kind, entry, definition)
        self.used.update(node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name))
        self.used.update(node.arg for node in ast.walk(self.tree) if isinstance(node, ast.arg))
        self.used.update(
            node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)
        )
        self.used.update(self.strings)
        self.used.update(
            binding.name for node in ast.walk(self.tree) for binding in self.node_bindings(node)
        )
        self.used.update(
            node.arg
            for node in ast.walk(self.tree)
            if isinstance(node, ast.keyword) and node.arg is not None
        )
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
        # Resolve everything against the original tree before mutating any name.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute) and node.attr in member_names:
                targets = self.attribute_bindings(node)
                affected = targets & watched
                if affected:
                    if len(targets) != 1:
                        raise ValueError(f"Ambiguous polymorphic attribute {ast.unparse(node)!r}")
                    owners, _ = self.receiver(node.value)
                    for owner in owners:
                        self.check_receiver(owner)
                    member_binding = next(iter(affected))
                    if member_binding in parameter_methods and not isinstance(node.ctx, ast.Load):
                        raise ValueError(
                            f"Reassigned method with allowlisted parameters: {ast.unparse(node)!r}"
                        )
                    if member_binding in renamed:
                        attributes[node] = renamed[member_binding]
                elif not targets:
                    raise ValueError(
                        f"Unresolved receiver for allowlisted attribute {ast.unparse(node)!r}"
                    )
            if isinstance(node, ast.AnnAssign):
                for reference in self.quoted_annotations(node.annotation):
                    if isinstance(reference.value, str):
                        with suppress(SyntaxError):
                            quoted = ast.parse(reference.value, mode="eval")
                            names = {
                                ref.id for ref in ast.walk(quoted) if isinstance(ref, ast.Name)
                            }
                            if names & {binding.name for binding in selected}:
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
        for binding, (_, _, definition) in selected.items():
            if isinstance(definition, ast.arg):
                binding.references.remove(definition)
                binding.rename(renamed[binding])
                binding.references.append(definition)
                definition.arg = renamed[binding]
            else:
                binding.rename(renamed[binding])
        for node, name in attributes.items():
            node.attr = name
        for node, name in keywords.items():
            if isinstance(node, ast.keyword):
                node.arg = name
            else:
                node.value = name
        return {entry: renamed[binding] for binding, (_, entry, _) in selected.items()}

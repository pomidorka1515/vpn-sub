from __future__ import annotations

import ast
import builtins
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import python_minifier

from .analysis import inspects_frames
from .cleanup import CollapseLiterals, ReleaseCleanup, strip_typing_imports
from .linker import flatten, module_name
from .renamers.allowlist import AllowlistRenamer
from .renamers.base import ArgumentRenamer
from .renamers.methods import PrivateMethodRenamer, rename_private_classes

if TYPE_CHECKING:
    from collections import Counter

__all__: list[str] = [
    "ObfuscationResult",
    "minify_tree",
    "obfuscate",
    "obfuscate_modules",
    "prepare_tree",
]


@dataclass(frozen=True)
class ObfuscationResult:
    source: str
    renames: dict[str, str]
    renamed_parameters: int
    skipped_parameters: Counter[str]
    renamed_methods: int


def obfuscate_modules(originals: dict[Path, str], entries: list[str]) -> ObfuscationResult:
    """Prepare, link, rename, and minify a set of relative application sources."""
    if not originals:
        raise ValueError("No application Python modules found")
    trees: dict[str, ast.Module] = {}
    generic_classes: set[str] = set()
    parsed: dict[Path, ast.Module] = {}
    module_names: set[str] = set()
    for relative, original in originals.items():
        name = module_name(relative)
        if name in module_names:
            raise ValueError(f"ambiguous module name: {name}")
        module_names.add(name)
        parsed[relative] = ast.parse(original, filename=str(relative))
        parsed[relative].__dict__["is_package"] = relative.name == "__init__.py"
        generic_classes.update(
            node.name
            for node in ast.walk(parsed[relative])
            if isinstance(node, ast.ClassDef) and node.type_params
        )
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
    # Analyze the final lexical scope: module-qualified data references no
    # longer look like reflective escapes, and every internal caller is linked.
    linked = flatten(trees)
    linked_trees = {"__main__": linked}
    methods = PrivateMethodRenamer(linked_trees)
    methods.run()
    renamer = ArgumentRenamer(linked_trees)
    renamer.run()
    renames = AllowlistRenamer(linked).apply(entries)
    return ObfuscationResult(
        source=minify_tree(linked, "app.min.py", rename_globals=True),
        renames=renames,
        renamed_parameters=renamer.renamed,
        skipped_parameters=renamer.skipped,
        renamed_methods=methods.renamed,
    )


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

    tree = cast(ast.Module, CollapseLiterals(cleanup).visit(tree))
    tree = cleanup.visit(tree)
    tree = strip_typing_imports(tree, cleanup.typing_modules)
    ast.fix_missing_locations(tree)
    return tree


def minify_tree(
    tree: ast.Module, filename: str, *, rename_globals: bool = False, rename_locals: bool = True
) -> str:
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
        rename_locals=rename_locals and not reflected,
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
    rename_private_classes(tree)
    PrivateMethodRenamer({"__main__": tree}).run()
    ArgumentRenamer({"__main__": tree}).run()
    return minify_tree(tree, filename)

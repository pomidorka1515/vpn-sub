from __future__ import annotations

import ast
import builtins
import multiprocessing
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

import python_minifier

from .aliases import consolidate_aliases, shorten_imports
from .analysis import inspects_frames
from .annotations import AnnotationIndex
from .cleanup import CollapseLiterals, ReleaseCleanup, strip_typing_imports
from .constants import inline_constants
from .dataclasses import minify_dataclasses
from .linker import flatten, module_name
from .markers import strip_markers
from .mixins import fold_mixins, mark_mixin_origins
from .parallel import default_jobs, ordered_map
from .renamers.allowlist import AllowlistRenamer
from .renamers.base import ArgumentRenamer
from .renamers.methods import (
    PrivateMethodRenamer,
    rename_constructor_classes,
    rename_private_classes,
)
from .sql import minify_sql_literals

if TYPE_CHECKING:
    from collections import Counter
    from collections.abc import Callable

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


def obfuscate_modules(
    originals: dict[Path, str],
    entries: list[str],
    *,
    jobs: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> ObfuscationResult:
    """Prepare, link, rename, and minify a set of relative application sources."""
    if not originals:
        raise ValueError("No application Python modules found")
    workers = default_jobs() if jobs is None else jobs
    if workers < 1:
        raise ValueError("jobs must be at least 1")
    workers = min(workers, len(originals))
    # Spawn is safe for library callers with threads, and does not inherit the
    # parent's application resources. Reuse the pool across all independent passes.
    with (
        ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        if workers > 1
        else nullcontext(None)
    ) as executor:
        return _obfuscate_modules(originals, entries, executor, progress)


type ProtocolKey = tuple[str, str]


def _parse_module(
    item: tuple[Path, str],
) -> tuple[ast.Module, set[str], set[ProtocolKey], list[tuple[ProtocolKey, ProtocolKey]]]:
    relative, original = item
    tree = ast.parse(original, filename=str(relative))
    tree.__dict__["is_package"] = relative.name == "__init__.py"
    name = module_name(relative)
    facts = ReleaseCleanup(tree, module=name, discovery_only=True)
    return (
        tree,
        facts.generic_classes,
        {(name, protocol) for protocol in facts.protocol_names},
        [(base, (name, derived)) for base, derived in facts.protocol_edges],
    )


def _prepare_module(
    item: tuple[Path, ast.Module],
    *,
    generic_classes: set[str],
    protocol_classes: set[tuple[str, str]],
) -> ast.Module:
    relative, tree = item
    return prepare_tree(
        "",
        str(relative),
        generic_classes,
        module=module_name(relative),
        protocol_classes=protocol_classes,
        tree=tree,
    )


def _obfuscate_modules(
    originals: dict[Path, str],
    entries: list[str],
    executor: ProcessPoolExecutor | None,
    progress: Callable[[str], None] | None,
) -> ObfuscationResult:
    def announce(stage: str) -> None:
        if progress is not None:
            progress(stage)

    announce(f"Parsing {len(originals)} application modules")
    generic_classes: set[str] = set()
    protocol_classes: set[ProtocolKey] = set()
    protocol_children: dict[ProtocolKey, list[ProtocolKey]] = {}
    parsed: dict[Path, ast.Module] = {}
    module_names: set[str] = set()
    for relative in originals:
        name = module_name(relative)
        if name in module_names:
            raise ValueError(f"ambiguous module name: {name}")
        module_names.add(name)
    for relative, (tree, classes, protocols, edges) in zip(
        originals, ordered_map(executor, _parse_module, originals.items()), strict=True
    ):
        parsed[relative] = tree
        generic_classes.update(classes)
        protocol_classes.update(protocols)
        for base, derived in edges:
            protocol_children.setdefault(base, []).append(derived)
    # Protocol inheritance can cross module boundaries, so discover it until
    # importing a protocol from another module adds no new protocol classes.
    announce("Discovering protocols and preparing modules")
    pending = deque(protocol_classes)
    while pending:
        for derived in protocol_children.get(pending.popleft(), ()):
            if derived not in protocol_classes:
                protocol_classes.add(derived)
                pending.append(derived)
    prepared = ordered_map(
        executor,
        partial(
            _prepare_module, generic_classes=generic_classes, protocol_classes=protocol_classes
        ),
        parsed.items(),
    )
    trees = {module_name(relative): tree for relative, tree in zip(parsed, prepared, strict=True)}
    announce("Linking modules and folding mixins")
    # Analyze the final lexical scope: module-qualified data references no
    # longer look like reflective escapes, and every internal caller is linked.
    linked = flatten(trees)
    # Analyze actual concrete method bodies, including lowered super dispatch,
    # rather than aliases to functions or the former mixin hierarchy.
    fold_mixins(linked, trees)
    # Folding introduces new lexical definitions. Track their original names
    # before automatic passes, just as the linker does for existing definitions.
    for node in ast.walk(linked):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.__dict__.setdefault("original_name", node.name)
        elif isinstance(node, ast.Name):
            node.__dict__.setdefault("original_name", node.id)
        elif isinstance(node, ast.arg):
            node.__dict__.setdefault("original_name", node.arg)
    announce("Analyzing dataclasses")
    minify_dataclasses(linked)
    linked_trees = {"__main__": linked}
    announce("Renaming private methods and parameters")
    methods = PrivateMethodRenamer(linked_trees)
    methods.run()
    renamer = ArgumentRenamer(linked_trees)
    renamer.run()
    announce("Applying the rename allowlist")
    renames = AllowlistRenamer(linked).apply(entries) if entries else {}
    announce("Minifying and validating app.min.py")
    inline_constants(linked)
    consolidate_aliases(linked)
    shorten_imports(linked)
    return ObfuscationResult(
        source=minify_tree(linked, "app.min.py", rename_globals=True, executor=executor),
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
    tree: ast.Module | None = None,
) -> ast.Module:
    """Remove release metadata before argument analysis and minification."""
    if tree is None:
        tree = ast.parse(source, filename=filename)
        tree.__dict__["is_package"] = Path(filename).name == "__init__.py"
    strip_markers(tree, filename)
    # Annotations are type information for the renamers, not release output.
    AnnotationIndex(
        module,
        tree,
        is_package=bool(tree.__dict__.get("is_package", False)),
    ).apply()
    mark_mixin_origins(tree, module)
    cleanup = ReleaseCleanup(
        tree,
        generic_classes,
        module=module,
        protocol_classes=protocol_classes,
    )

    tree = cast(ast.Module, CollapseLiterals(cleanup).visit(tree))
    tree = cast(ast.Module, cleanup.visit(tree))
    tree = strip_typing_imports(tree, cleanup.typing_modules)
    original_file = Path(filename).as_posix()
    for node in ast.walk(tree):
        node.__dict__["original_file"] = original_file
    ast.fix_missing_locations(tree)
    return tree


def minify_tree(
    tree: ast.Module,
    filename: str,
    *,
    rename_globals: bool = False,
    rename_locals: bool = True,
    executor: ProcessPoolExecutor | None = None,
) -> str:
    """Shorten locals and, after linking, globals without changing runtime APIs."""
    minify_sql_literals(tree, executor=executor)
    rename_constructor_classes(tree)
    source = ast.unparse(tree)
    preserved: set[str] = set()
    for node in ast.walk(tree):
        # Keep the planned class/function/parameter spellings: logs, callbacks
        # and keyword calls depend on these. Constructor-only classes and closed
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
    minify_dataclasses(tree)
    rename_private_classes(tree)
    PrivateMethodRenamer({"__main__": tree}).run()
    ArgumentRenamer({"__main__": tree}).run()
    return minify_tree(tree, filename)

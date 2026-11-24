from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from obfuscator.linker import flatten
from obfuscator.pipeline import minify_tree, prepare_tree
from obfuscator.renamers.allowlist import AllowlistRenamer
from obfuscator.renamers.base import ArgumentRenamer

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__: list[str] = ["allowlisted", "flattened_modules", "renamed_modules"]


def renamed_modules(sources: Mapping[str, str]) -> dict[str, str]:
    trees = {name: ast.parse(source) for name, source in sources.items()}
    ArgumentRenamer(trees).run()
    return {name: ast.unparse(tree) for name, tree in trees.items()}


def flattened_modules(sources: Mapping[str, str]) -> str:
    """Test linking in isolation from the pre-existing obfuscation passes."""
    trees = {
        name: prepare_tree(source, name.replace(".", "/") + ".py", module=name)
        for name, source in sources.items()
    }
    return ast.unparse(flatten(trees))


def allowlisted(source: str, entries: list[str]) -> tuple[str, dict[str, str]]:
    tree = prepare_tree(source, "fixture.py")
    tree = flatten({"main": tree}, rename_classes=False)
    renames = AllowlistRenamer(tree).apply(entries)
    return minify_tree(tree, "fixture.py", rename_locals=False), renames

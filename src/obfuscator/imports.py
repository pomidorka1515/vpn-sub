from __future__ import annotations

import ast
from contextlib import suppress
from typing import cast

from .analysis import inspects_frames

__all__: list[str] = ["optimize_imports"]


def optimize_imports(tree: ast.Module) -> None:
    """Prune and group the leading import table of a flattened release."""
    end = 0
    while end < len(tree.body) and isinstance(tree.body[end], (ast.Import, ast.ImportFrom)):
        end += 1
    imports = cast(list[ast.Import | ast.ImportFrom], tree.body[:end])
    if not imports:
        return

    # Include stores/deletes conservatively: removing an import can otherwise
    # change unbound-name behavior. Quoted field annotations are evaluated later.
    used: set[str] = set()
    reflective = inspects_frames(tree) or bool(getattr(tree, "tainted", False))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"globals", "locals", "vars", "eval", "exec"}
        ):
            reflective = True
        elif isinstance(node, ast.AnnAssign):
            pending = [node.annotation]
            while pending:
                for item in ast.walk(pending.pop()):
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        with suppress(SyntaxError, ValueError):
                            expression = ast.parse(item.value, mode="eval")
                            used.update(n.id for n in ast.walk(expression) if isinstance(n, ast.Name))
                            pending.append(expression.body)

    # Dotted imports without an alias bind their root but also load a child.
    # The linker emits a separate child import; retain it while the root is live.
    children: dict[str, set[str]] = tree.__dict__.get("submodule_imports", {})
    grouped: list[ast.stmt] = []
    plain: ast.Import | None = None
    from_modules: dict[tuple[str | None, int], ast.ImportFrom] = {}
    for statement in imports:
        if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
            grouped.append(statement)
            continue
        names = [
            alias
            for alias in statement.names
            if reflective
            or alias.name == "*"
            or (alias.asname or alias.name.split(".")[0]) in used
            or bool(children.get(alias.asname or alias.name.split(".")[0], set()) & used)
        ]
        if not names:
            continue
        if isinstance(statement, ast.Import):
            if plain is None:
                plain = statement
                plain.names = names
                grouped.append(plain)
            else:
                plain.names.extend(names)
        else:
            key = (statement.module, statement.level)
            if key not in from_modules or any(alias.name == "*" for alias in names):
                statement.names = names
                grouped.append(statement)
                if not any(alias.name == "*" for alias in names):
                    from_modules[key] = statement
            else:
                from_modules[key].names.extend(names)
    tree.body = grouped + tree.body[end:]

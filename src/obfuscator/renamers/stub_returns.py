from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

from ..annotations import AnnotationIndex, TypeHint, merge_hints

__all__: list[str] = []


class StubReturns:
    """Read pinned mypy's stdlib stubs for APIs with no Python annotations.

    Stubs are parsed, never imported or evaluated. Every structurally viable
    overload must have a known result; unsupported generics remain unknown.
    """

    def __init__(self) -> None:
        spec = find_spec("mypy")
        self.root: Path | None = Path(spec.origin).parent / "typeshed" / "stdlib" if spec is not None and spec.origin is not None else None
        self.modules: dict[str, tuple[AnnotationIndex, ast.Module] | None] = {}

    def module(self, name: str) -> tuple[AnnotationIndex, ast.Module] | None:
        if name in self.modules:
            return self.modules[name]
        self.modules[name] = None
        if self.root is None or not all(part.isidentifier() for part in name.split(".")):
            return None
        base = self.root.joinpath(*name.split("."))
        for path in (base.with_suffix(".pyi"), base / "__init__.pyi"):
            if not path.is_file():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                return None
            index = AnnotationIndex(name, tree, is_package=path.name == "__init__.pyi")
            index.apply()
            self.modules[name] = index, tree
            return index, tree
        return None

    @staticmethod
    def accepts(function: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call | None) -> bool:
        if call is None:
            return True
        if any(isinstance(argument, ast.Starred) for argument in call.args) or any(keyword.arg is None for keyword in call.keywords):
            return False
        positional = [*function.args.posonlyargs, *function.args.args]
        if len(call.args) > len(positional) and function.args.vararg is None:
            return False
        supplied = {argument.arg for argument in positional[:len(call.args)]}
        keywords = {keyword.arg for keyword in call.keywords}
        if supplied & keywords or keywords & {argument.arg for argument in function.args.posonlyargs}:
            return False
        allowed = {argument.arg for argument in [*function.args.args, *function.args.kwonlyargs]}
        if function.args.kwarg is None and not keywords <= allowed:
            return False
        supplied.update(keyword for keyword in keywords if keyword is not None)
        required = positional[:len(positional) - len(function.args.defaults)]
        required.extend(argument for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True) if default is None)
        return all(argument.arg in supplied for argument in required)

    def returned(self, path: str, call: ast.Call | None, seen: frozenset[str] = frozenset()) -> TypeHint:
        if path in seen:
            return TypeHint()
        seen |= {path}
        module, _, name = path.rpartition(".")
        loaded = self.module(module)
        if loaded is None:
            return TypeHint()
        index, tree = loaded
        functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

        def collect(body: list[ast.stmt]) -> None:
            for statement in body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name:
                    functions.append(statement)
                elif isinstance(statement, ast.If):
                    collect(statement.body)
                    collect(statement.orelse)

        collect(tree.body)
        if functions:
            hints: list[TypeHint] = []
            for function in functions:
                if self.accepts(function, call):
                    hint = function.__dict__.get("return_type")
                    if not isinstance(hint, TypeHint) or not hint or hint.incomplete:
                        return TypeHint()
                    hints.append(TypeHint(origin="awaitable", arguments=(hint,)) if isinstance(function, ast.AsyncFunctionDef) else hint)
            return merge_hints(*hints)
        target = index.names.get(name)
        if target is not None and target != path:
            return self.returned(target, call, seen)
        stars = [statement.module for statement in tree.body if isinstance(statement, ast.ImportFrom) and statement.module is not None and not statement.level and any(alias.name == "*" for alias in statement.names)]
        if len(stars) == 1:
            return self.returned(f"{stars[0]}.{name}", call, seen)
        return TypeHint()

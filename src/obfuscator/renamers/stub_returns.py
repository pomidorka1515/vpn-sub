from __future__ import annotations

import ast
import builtins
import inspect
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

    def literal_match(self, annotation: ast.expr | None, value: object, module: str, seen: frozenset[str] = frozenset()) -> bool | None:
        """Prove whether a literal satisfies a stub annotation, without evaluation."""
        if annotation is None:
            return None
        if isinstance(annotation, ast.Constant) and annotation.value is None:
            return value is None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            parts = [self.literal_match(part, value, module, seen) for part in (annotation.left, annotation.right)]
            return True if True in parts else None if None in parts else False
        if isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name) and annotation.value.id == "Literal":
            elements = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            try:
                values = [ast.literal_eval(element) for element in elements]
            except (ValueError, TypeError):
                return None
            return any(type(value) is type(item) and value == item for item in values)
        if not isinstance(annotation, ast.Name):
            return None
        builtin = vars(builtins).get(annotation.id)
        if builtin in {str, bytes, int, float, bool}:
            if type(value) is builtin:
                return True
            # Numeric subtyping and coercion need more information than this
            # literal-only matcher provides; never discard such an overload.
            if builtin in {int, float} and type(value) in {bool, int, float}:
                return None
            return False
        loaded = self.module(module)
        if loaded is None:
            return None
        index, _tree = loaded
        path = index.names.get(annotation.id, f"{module}.{annotation.id}")
        if path in seen:
            return None
        target_module, _, name = path.rpartition(".")
        target = self.module(target_module)
        if target is None:
            return None
        for statement in target[1].body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == name and statement.value is not None:
                return self.literal_match(statement.value, value, target_module, seen | {path})
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == name:
                return self.literal_match(statement.value, value, target_module, seen | {path})
        return None

    def method(self, owner: type, name: str, call: ast.Call | None, receiver: TypeHint, *, overloaded_only: bool = False) -> TypeHint:
        """Read method results from runtime MRO stubs, never invoke descriptors."""
        for base in owner.__mro__:
            override = vars(base).get(name)
            opaque_override = name in vars(base) and not inspect.ismethoddescriptor(override)
            loaded = self.module(base.__module__)
            if loaded is None:
                if name in vars(base):
                    return TypeHint()
                continue
            _, tree = loaded
            classes: list[ast.ClassDef] = []

            def collect_classes(body: list[ast.stmt]) -> None:
                for statement in body:
                    if isinstance(statement, ast.ClassDef) and statement.name == base.__name__:  # ruff: ignore[function-uses-loop-variable] - Called in this iteration.
                        classes.append(statement)  # ruff: ignore[function-uses-loop-variable] - Called in this iteration.
                    elif isinstance(statement, ast.If):
                        collect_classes(statement.body)
                        collect_classes(statement.orelse)

            collect_classes(tree.body)
            if not classes:
                if name in vars(base):
                    return TypeHint()
                continue
            functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

            def collect(body: list[ast.stmt]) -> None:
                for statement in body:
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name:
                        functions.append(statement)  # ruff: ignore[function-uses-loop-variable] - Called in this iteration.
                    elif isinstance(statement, ast.If):
                        collect(statement.body)
                        collect(statement.orelse)

            for definition in classes:
                before = len(functions)
                collect(definition.body)
                if len(classes) > 1 and len(functions) == before:
                    return TypeHint()
            if not functions:
                if opaque_override:
                    return TypeHint()
                continue
            # Bind an explicit placeholder for the instance parameter.
            bound_call = ast.Call(func=ast.Name(id="_", ctx=ast.Load()), args=[ast.Constant(value=None), *(call.args if call else [])], keywords=call.keywords if call else [])
            candidates = [function for function in functions if self.accepts(function, bound_call)]
            overloaded = bool(candidates) and all(
                function.decorator_list and all(
                    isinstance(decorator, ast.Name) and loaded[0].names.get(decorator.id) == "typing.overload"
                    for decorator in function.decorator_list
                ) for function in candidates
            )
            if not overloaded and any(function.decorator_list for function in candidates):
                return TypeHint()
            if overloaded_only and not overloaded:
                return TypeHint()
            hints: list[TypeHint] = []
            for function in candidates:
                if overloaded:
                    positional = [*function.args.posonlyargs, *function.args.args][1:]
                    if call is not None and len(call.args) > len(positional):
                        return TypeHint()
                    supplied = list(zip(positional, call.args if call else [], strict=False))
                    allowed = {parameter.arg: parameter for parameter in [*positional, *function.args.kwonlyargs]}
                    if call is not None and any(keyword.arg not in allowed for keyword in call.keywords):
                        return TypeHint()
                    supplied.extend((allowed[keyword.arg], keyword.value) for keyword in (call.keywords if call else []) if keyword.arg in allowed)
                    matches = [self.literal_match(parameter.annotation, argument.value, base.__module__) if isinstance(argument, ast.Constant) else None for parameter, argument in supplied]
                    if False in matches:
                        continue
                    if None in matches:
                        return TypeHint()
                hint = function.__dict__.get("return_type")
                if not isinstance(hint, TypeHint) or not hint or hint.incomplete:
                    return TypeHint()
                if hint.self_reference:
                    hint = receiver
                hints.append(TypeHint(origin="awaitable", arguments=(hint,)) if isinstance(function, ast.AsyncFunctionDef) else hint)
                if overloaded:
                    break
            return merge_hints(*hints)
        return TypeHint()

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

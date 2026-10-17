from collections.abc import Callable

class JsonSchemaValueException(ValueError):  # ruff: ignore[error-suffix-on-exception-name]
    message: str
    path: list[str]

def compile[T](  # ruff: ignore[builtin-variable-shadowing]
    definition: dict[str, T] | bool,
    *,
    use_default: bool = True,
    use_formats: bool = True,
) -> Callable[[object], object]: ...

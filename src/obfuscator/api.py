from __future__ import annotations

__all__: list[str] = ["main", "obfuscate"]


def main() -> None:
    from .cli import main as run

    run()


def obfuscate(source: str, filename: str) -> str:
    from .pipeline import obfuscate as run

    return run(source, filename)

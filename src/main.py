"""Process entrypoint for the subscription service.

Works under plain Python (``python -m main`` with ``src`` on ``PYTHONPATH``)
and as a Nuitka binary. Both paths run Hypercorn in-process on one event loop.
``APP_BIND`` selects the listener, with ``GUNICORN_BIND`` as a temporary fallback.
"""

from __future__ import annotations

import asyncio

from paths import compiled


def main() -> None:
    from cli.updater import notice

    notice()
    asyncio.run(serve_application())


async def serve_application() -> None:
    from hypercorn.asyncio import serve  # pyright: ignore[reportUnknownVariableType]  # Upstream WSGI signature is untyped.

    from asgi import app
    from serve import server_config

    # Hypercorn owns SIGTERM/SIGINT and its shutdown trigger in both entrypoints.
    await serve(app, server_config())


def probe() -> None:
    """Import the async stack and verify bundled resources without starting it.

    ASGI import registers lifespan hooks but opens no runtime connections.
    Missing async dependencies or bundled resources fail the probe.
    """
    from importlib import import_module

    from paths import bundled_root

    for module in ("asgi", "hypercorn.asyncio", "session.async_transport", "db.async_database"):
        import_module(module)

    root = bundled_root()
    language = root / "lang.jsonc"
    fonts = root / "res"
    if not language.is_file():
        raise SystemExit(f"language file not found: {language}")
    if not fonts.is_dir():
        raise SystemExit(f"res directory not found: {fonts}")
    if compiled():
        schema = root / "config.schema.json"
        if not schema.is_file():
            raise SystemExit(f"schema file not found: {schema}")


if __name__ == "__main__":
    from cli import dispatch

    dispatch("vpn-sub", probe)
    main()

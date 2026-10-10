"""Process entrypoint for the subscription service.

Works under plain Python (``python -m main`` with ``src`` on ``PYTHONPATH``)
and as a Nuitka binary. Both paths run gunicorn in-process with the settings
in ``serve``. Do not shell out to ``venv/bin/gunicorn``: a frozen binary has
no interpreter and no config file to hand it.
"""

from __future__ import annotations

from wsgiref.types import WSGIApplication

from gunicorn.app.base import BaseApplication
from gunicorn.util import import_app

from paths import compiled
from serve import options


class Application(BaseApplication):
    def load_config(self) -> None:
        assert self.cfg is not None
        for key, value in options().items():
            self.cfg.set(key, value)

    def load(self) -> WSGIApplication:  # type: ignore[override]
        assert self.cfg is not None
        return import_app(self.cfg.wsgi_app)  # type: ignore[return-value]


def main() -> None:
    from cli.updater import notice
    from loggers import Logger

    notice(Logger("updater"))
    Application("%(prog)s", prog="vpn-sub").run()


def probe() -> None:
    """Prove the payload can see shipped files, then exit.

    Does not import ``wsgi``. That module calls ``create_application()`` and
    would require Redis, ``config.json``, and a panel. A missing import fails
    before this function runs, because this module imports gunicorn. A payload
    that cannot see ``res/``, ``lang.jsonc``, or ``config.schema.json`` fails here.
    """
    from paths import bundled_root

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
    from loggers import Logger

    dispatch("vpn-sub", probe, Logger("updater"))
    main()

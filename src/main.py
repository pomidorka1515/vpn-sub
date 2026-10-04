"""Process entrypoint for the subscription service.

Works under plain Python (``python -m main`` with ``src`` on ``PYTHONPATH``)
and as a Nuitka binary. Both paths run gunicorn in-process with the settings
in ``serve``. Do not shell out to ``venv/bin/gunicorn``: a frozen binary has
no interpreter and no config file to hand it.
"""

from __future__ import annotations

from gunicorn.app.base import BaseApplication
from gunicorn.util import import_app
from wsgiref.types import WSGIApplication

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
    Application("%(prog)s", prog="vpn-sub").run()


if __name__ == "__main__":
    main()

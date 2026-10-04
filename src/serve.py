"""Gunicorn settings shared by the CLI config and the frozen entrypoint.

``src/gunicorn.conf.py`` is what ``venv/bin/gunicorn --config`` imports.
``src/main.py`` applies the same values in-process, because a Nuitka binary
has no ``venv/bin/gunicorn`` and cannot load that config file as source.
Keep the two callers on this module so the settings cannot drift.
"""

from __future__ import annotations

import os
from concurrent import futures

from gunicorn.workers.gthread import ThreadWorker

from loggers.access import GunicornLogger

__all__ = ["NamedThreadWorker", "options"]


class NamedThreadWorker(ThreadWorker):
    def get_thread_pool(self) -> futures.ThreadPoolExecutor:
        return futures.ThreadPoolExecutor(
            max_workers=self.cfg.threads,
            thread_name_prefix="gunicorn",  # 'ThreadPoolWorker-N_N' is ugly
        )


def options() -> dict[str, object]:
    """Settings gunicorn accepts as config keys.

    ``worker_class`` is an import path, not the class. The config validator
    accepts a class, and ``gunicorn.conf`` passes ``NamedThreadWorker``
    directly because gunicorn loads that file itself. ``main`` sets the key
    through ``cfg.set`` and must pass a string: a class object is not a
    stable value to round-trip through gunicorn's config, and the import
    path stays loadable under plain Python and inside a binary that
    compiled this module.
    """
    return {
        "worker_class": "serve.NamedThreadWorker",
        "threads": 3,
        "workers": 1,
        "limit_request_line": 0,
        "capture_output": True,
        "accesslog": "-",
        "errorlog": "-",
        "logger_class": GunicornLogger,
        "wsgi_app": "wsgi:app",
        "graceful_timeout": 10,
        "bind": os.getenv("GUNICORN_BIND", "127.0.0.1:5550"),
    }

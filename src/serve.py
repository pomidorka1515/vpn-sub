"""Shared Hypercorn configuration for checkout and compiled serving.

Checkout: ``hypercorn --config python:serve asgi:app``.
Gunicorn environment aliases expire at migration step 12 cutover.
"""

from __future__ import annotations

import os

from hypercorn.config import Config

HTTP_DRAIN_TIMEOUT = 10
RUNTIME_SHUTDOWN_TIMEOUT = 60
STARTUP_TIMEOUT = 120


def server_config() -> Config:
    config = Config()
    config.bind = [os.getenv("APP_BIND", os.getenv("GUNICORN_BIND", "127.0.0.1:5550"))]
    config.workers = 1
    config.worker_class = "asyncio"
    config.accesslog = None  # Privacy-preserving API middleware owns access logs.
    config.errorlog = "-"
    config.graceful_timeout = HTTP_DRAIN_TIMEOUT
    config.startup_timeout = STARTUP_TIMEOUT
    config.shutdown_timeout = RUNTIME_SHUTDOWN_TIMEOUT + 5
    return config


# Hypercorn's python: loader reads these attributes; programmatic serving uses
# the same factory. Importing configuration opens no service resources.
_config = server_config()
bind = _config.bind
workers = _config.workers
worker_class = _config.worker_class
accesslog = _config.accesslog
errorlog = _config.errorlog
graceful_timeout = _config.graceful_timeout
startup_timeout = _config.startup_timeout
shutdown_timeout = _config.shutdown_timeout

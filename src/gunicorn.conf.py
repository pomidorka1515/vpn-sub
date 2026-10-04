"""Gunicorn config file. Settings live in ``serve`` so ``main`` uses the same ones.

``worker_class`` is the class object here. ``main`` passes the import path
instead; see ``serve.options``.
"""

from serve import NamedThreadWorker, options

_options = options()

worker_class = NamedThreadWorker
threads = _options["threads"]
workers = _options["workers"]
limit_request_line = _options["limit_request_line"]
capture_output = _options["capture_output"]
accesslog = _options["accesslog"]
errorlog = _options["errorlog"]
logger_class = _options["logger_class"]
wsgi_app = _options["wsgi_app"]
graceful_timeout = _options["graceful_timeout"]
bind = _options["bind"]

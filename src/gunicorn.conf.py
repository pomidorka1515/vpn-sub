import os
from concurrent import futures

from gunicorn.workers.gthread import ThreadWorker

from loggers import GunicornLogger

class NamedThreadWorker(ThreadWorker):
    def get_thread_pool(self) -> futures.ThreadPoolExecutor:
        return futures.ThreadPoolExecutor(
            max_workers=self.cfg.threads,
            thread_name_prefix="gunicorn" # 'ThreadPoolWorker-N_N' is ugly
        )

worker_class = NamedThreadWorker

threads = 3
workers = 1
limit_request_line = 0
capture_output = True
accesslog="-"
errorlog="-"
logger_class = GunicornLogger
wsgi_app="wsgi:app"
graceful_timeout = 10
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:5550")

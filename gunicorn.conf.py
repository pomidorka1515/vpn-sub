from gunicorn.workers.gthread import ThreadWorker
from concurrent import futures

class NamedThreadWorker(ThreadWorker):
    def get_thread_pool(self) -> futures.ThreadPoolExecutor:
        return futures.ThreadPoolExecutor(
            max_workers=self.cfg.threads,
            thread_name_prefix="gunicorn" # 'ThreadPoolWorker-N_N' is ugly
        )

worker_class = NamedThreadWorker
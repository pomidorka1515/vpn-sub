from config import Config
import threading, time, random

c = Config('/tmp/test_cfg.json')
c.update(lambda d: d.setdefault('counter', 0))

def hammer():
    for _ in range(100):
        with c.edit() as tx:
            tx['counter'] = tx['counter'] + 1

threads = [threading.Thread(target=hammer) for _ in range(10)]
for t in threads: t.start()
for t in threads: t.join()

print(c['counter'])  # should be exactly 1000

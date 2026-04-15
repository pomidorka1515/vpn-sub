import time
from config import Config

c = Config('/var/www/sub/config.json')

# Single deepcopy cost
t = time.perf_counter()
for _ in range(100):
    _ = c['users']
elapsed = time.perf_counter() - t
print(f"100 reads of 'users': {elapsed*1000:.2f}ms ({elapsed*10:.2f}ms each)")

# Transaction cost
t = time.perf_counter()
for i in range(100):
    with c.edit() as tx:
        tx['_notified'].append('test')
        tx['_notified'].pop()
elapsed = time.perf_counter() - t
print(f"100 mutating transactions: {elapsed*1000:.2f}ms ({elapsed*10:.2f}ms each)")

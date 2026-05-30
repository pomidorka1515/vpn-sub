from core import BandwidthSnapshot
from chart import *
import time
import os

snaps = [
    BandwidthSnapshot(
        ts=int(time.time()) - i * 86400,
        up=int(1e5 * (i % 3 + 1)),
        down=int(3e15 * (i % 4 + 1)),
        wl_up=int(5e10 * (i % 2 + 1)),
        wl_down=int(2e15 * (i % 3 + 1)),
    )
    for i in range(14)
]

lang = input("Enter lang, defualts to en\n")
if lang not in ('ru', 'en'):
    lang = "en"
path = input("Enter path\n")
if not path:
    path = "/var/www/static/other/"
img = bandwidth_chart(snaps, label='VOIDBOUND XOOBERT V. GONGLER',lang= lang)
assert img is not None
with open(os.path.join(path, 'test-chart.png'), 'wb') as f:
    f.write(img.read())

fake_leaderboard = {
    "Alice": 5_000_000_000,
    "Bob": 3_500_000_000,
    "Charlie": 7_200_000_000,
    "Diana": 1_800_000_000,
    "Eve": 4_100_000_000,
}
for bw_type in ("total", "monthly", "wl_monthly"):
    for ln in ("en", "ru"):
        img = leaderboard_chart(fake_leaderboard, bandwidth_type=bw_type, lang=ln)
        assert img is not None, f"leaderboard_chart failed for {bw_type}/{ln}"
        with open(os.path.join(path, f'test-leaderboard-{bw_type}-{ln}.png'), 'wb') as f:
            f.write(img.read())

# Test empty data
img = leaderboard_chart({}, bandwidth_type="total", lang="en")
assert img is None, "leaderboard_chart should return None for empty data"

print("All chart tests passed!")

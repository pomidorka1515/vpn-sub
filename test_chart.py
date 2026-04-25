from core import BandwidthSnapshot
from chart import bandwidth_chart
import time
import os
# Fake 14 days of data
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
if lang not in ['ru', 'en']:
    lang = "en"
path = input("Enter path\n")
if not path:
    path = "/var/www/static/other/"
img = bandwidth_chart(snaps, label='VOIDBOUND XOOBERT V. GONGLER',lang= lang)
with open(os.path.join(path, 'test-chart.png'), 'wb') as f:
    f.write(img.read())

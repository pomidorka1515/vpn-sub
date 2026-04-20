from __future__ import annotations

from core import Subscription, BWatch, XUiSession, Response, Flask, request, send_file
from api import WebApi, Api
from bots import PublicBot, AdminBot
from config import Config
from loggers import Logger
import fcntl

##############################################################
### Startup sequence. Do not touch if you dont understand. ###
### Order matters. A fucking lot.                          ###
##############################################################

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024  # 64KB is plenty

log = Logger("other") # Logger for other stuff outside classes

cfg = Config(
    path="../config.json",
    indent=4,
    strict_schema=True,
    sync_mode="data",
    isolate_commits=True,

    backup_dir="./backup/"
)

# Panels.
# Add as many as yw, the loop will take care of them.

panels = []
wl = None
for i, v in cfg['3xui'].items():
    x = cfg['3xui']
    auth = x[i].get('nginx_auth', None)
    if auth is not None: auth = tuple(auth)
    if x[i]['whitelist']:
        wl = XUiSession(
            name=x[i]['name'],
            address=x[i]['address'],
            port=x[i]['port'],
            uri=x[i]['uri'],
            username=x[i]['username'],
            password=x[i]['password'],
            https=x[i]['https'],
            nginx_auth=auth,
            ignore_inbounds=x[i]['ignore_inbounds']
        )
        continue
    try:
        panels.append(XUiSession(
            name=x[i]['name'],
            address=x[i]['address'],
            port=x[i]['port'],
            uri=x[i]['uri'],
            username=x[i]['username'],
            password=x[i]['password'],
            https=x[i]['https'],
            nginx_auth=auth,
            ignore_inbounds=x[i]['ignore_inbounds']
        ))
    except Exception as e:
        log.critical(f"ERROR when initializing panel {i}: {str(e)}")

# Create instances
sub = Subscription(cfg=cfg, app=app, panels=panels, whitelist_panel=wl)
bw = BWatch(cfg=cfg, sub=sub)
api = Api(app=app, cfg=cfg, sub=sub, bw=bw)
webapi = WebApi(app=app, cfg=cfg, sub=sub, bw=bw)
adminbot = AdminBot(sub=sub, cfg=cfg)
bot = PublicBot(sub=sub, cfg=cfg)

# Multiple workers protection
_lock_fd = open('/tmp/sub_primary.lock', 'w')
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _is_primary = True
except OSError:
    _is_primary = False
    _lock_fd.close()

if _is_primary:
    bw.start()
    adminbot.start()
    bot.start()
    bw.bot = bot

# Loggers which are broadcasted to AdminBot
loggers = [
    log,
    sub.log,
    bw.log,
    api.log,
    webapi.log,
    adminbot.log,
    bot.log
]
for l in loggers:
    l.set_tg_bot(bot=adminbot)

log.info("Launch successful!")

if __name__ == '__main__': # Dev, gunicorn doesnt gaf
    app.run(port=5550)
    

from __future__ import annotations

import fcntl
import sys
import atexit
import threading
threading.main_thread().name = 'main'

from core import (
    Subscription, BWatch,
    Response, Flask, request, send_file
)
from session import XUiSession
from api import WebApi, Api
from bots import PublicBot, AdminBot
from config import Config
from loggers import Logger

##############################################################
### Startup sequence. Do not touch if you dont understand. ###
### Order matters. A lot.                                  ###
##############################################################

log = Logger("app")

def _build_panels(cfg: Config) -> tuple[list[XUiSession], XUiSession | None]:
    """Initialize all 3x-ui panel sessions from config."""
    panels: list[XUiSession] = []
    whitelist: XUiSession | None = None
    
    for name, panel_cfg in cfg['3xui'].items():
        kwargs = {
            'name': panel_cfg['name'],
            'address': panel_cfg['address'],
            'port': panel_cfg['port'],
            'uri': panel_cfg['uri'],
            'username': panel_cfg['username'],
            'password': panel_cfg['password'],
            'https': panel_cfg['https'],
            'nginx_auth': tuple(panel_cfg['nginx_auth']) if panel_cfg.get('nginx_auth') else None,
            'ignore_inbounds': tuple(panel_cfg.get('ignore_inbounds', [])),
            'inject_headers': panel_cfg.get('inject_headers'),
        }
        try:
            session = XUiSession(**kwargs)
        except Exception as e:
            log.critical(f"Failed to initialize panel '{name}': {e}")
            continue
        
        if panel_cfg['whitelist']:
            if whitelist is not None:
                log.warning(f"Multiple whitelist panels configured; using last one ({name})")
            whitelist = session
        else:
            panels.append(session)
    
    return panels, whitelist


def _acquire_primary_lock() -> bool:
    """Try to acquire the single-primary-worker lock. Returns True on success."""
    fd = open('/tmp/sub_primary.lock', 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log.info(f"I am the primary worker (pid={__import__('os').getpid()})")
        return True
    except OSError:
        fd.close()
        return False


# ------------------------------------------------------------
# Flask app
# ------------------------------------------------------------
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024  # 64KB is plenty

# ------------------------------------------------------------
# Configs (two separate files)
# ------------------------------------------------------------
_config_kwargs = {
    'read_only': False,
    'strict_schema': True,
    'sync_mode': 'data',
    'isolate_commits': True,
    'backup_dir': './backup/',
}

cfg = Config(path='../config.json', indent=4, **_config_kwargs)
bw_cfg = Config(path='../bw_history.json', indent=2, **_config_kwargs)

# ------------------------------------------------------------
# Panels
# ------------------------------------------------------------
panels, wl = _build_panels(cfg)

if not panels and wl is None:
    log.critical("No panels initialized. Cannot start.")
    sys.exit(1)

# ------------------------------------------------------------
# Wire up components
# ------------------------------------------------------------
sub      = Subscription(cfg=cfg, bw_cfg=bw_cfg, app=app, panels=panels, whitelist_panel=wl)
bw       = BWatch(cfg=cfg, bw_cfg=bw_cfg, sub=sub)
api      = Api(app=app, cfg=cfg, sub=sub, bw=bw)
webapi   = WebApi(app=app, cfg=cfg, sub=sub, bw=bw)
adminbot = AdminBot(sub=sub, cfg=cfg)
bot      = PublicBot(sub=sub, cfg=cfg)

bw.bot   = bot  # can't do in BWatch.__init__ because PublicBot needs sub first

# ------------------------------------------------------------
# Broadcast selected loggers to AdminBot
# ------------------------------------------------------------
loggers = (
    log, 
    
    sub.log, bw.log, 
    
    api.log, webapi.log,
    
    adminbot.log, bot.log
)
for l in loggers: l.set_tg_bot(bot=adminbot)
# ------------------------------------------------------------
# Single-primary-worker startup
# ------------------------------------------------------------
if _acquire_primary_lock():
    bw.start()
    adminbot.start()
    bot.start()
    log.info("Launch successful!")
    _is_primary = True
else:
    log.info("Secondary worker, skipping background tasks.")
    _is_primary = False
# ------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------
def _shutdown():
    log.info("Shutting down...")
    if _is_primary:
        bw.stop()
        adminbot.stop()
        bot.stop()
    for panel in panels:
        panel.close()
    if wl:
        wl.close()
    log.info("Shutdown complete.")
atexit.register(_shutdown)


if __name__ == '__main__':  # Dev only; gunicorn doesnt gaf
    app.run(port=5550)    

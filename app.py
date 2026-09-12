from __future__ import annotations

import fcntl
import sys
import os
import atexit
import threading
threading.main_thread().name = 'main'


from core import Subscription, BWatch
from session import XUiSession
from api import WebApi, Api
from bots import PublicBot, AdminBot
from config import Config, LinesConfig, SYNC_MODES
from db import Database
from loggers import Logger
from custom_types import ConfigLike

from flask import Flask
from typing import cast, TypedDict
from pathlib import Path

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
        try:
            session = XUiSession(
                name=panel_cfg['name'],
                address=panel_cfg['address'],
                port=panel_cfg['port'],
                uri=panel_cfg['uri'],
                username=panel_cfg['username'],
                password=panel_cfg['password'],
                https=panel_cfg['https'],
                nginx_auth=tuple(panel_cfg.get('nginx_auth', [])) or None,
                ignore_inbounds=tuple(panel_cfg.get('ignore_inbounds', [])),
                inject_headers=panel_cfg.get('inject_headers'),
            )
        except Exception:
            log.critical(f"Failed to initialize panel '{name}':")
            raise
        if panel_cfg['whitelist']:
            if whitelist is not None:
                log.warning(f"Multiple whitelist panels configured; using last one ({name})")
            whitelist = session
        else:
            panels.append(session)
    
    return panels, whitelist

_primary_lock_fd = None

def _acquire_primary_lock() -> bool:
    """Try to acquire the single-primary-worker lock. Returns True on success."""
    global _primary_lock_fd
    fd = open('/tmp/sub_primary.lock', 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _primary_lock_fd = fd
        log.info(f"I am the primary worker (pid={os.getpid()})")
        return True
    except OSError:
        fd.close()
        return False


# ------------------------------------------------------------
# Flask app
# ------------------------------------------------------------
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024  # 64KB is plenty
app.config['JSON_SORT_KEYS'] = False


# ------------------------------------------------------------
# Path configuration
# ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("DIR_DATA", PROJECT_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

BACKUP_DIR = Path(os.getenv("DIR_BACKUPS", DATA_DIR / "backup"))

CONFIG_PATH = Path(os.getenv("PATH_CONFIG", DATA_DIR / "config.json"))
LANG_PATH = Path(os.getenv("PATH_LANG", PROJECT_ROOT / "lang.jsonc"))
DB_PATH = Path(os.getenv("PATH_DB", DATA_DIR / "state.sqlite3"))
LOG_PATH = Path(os.getenv("PATH_LOG", DATA_DIR / "log.jsonl"))
AUDIT_PATH = Path(os.getenv("PATH_AUDIT", DATA_DIR / "audit.jsonl"))

# ------------------------------------------------------------
# Configs & databases
# ------------------------------------------------------------
class _BaseConfigKwargs(TypedDict):
    read_only: bool
    strict_schema: bool
    sync_mode: SYNC_MODES
    isolate_commits: bool
    backup_dir: str | Path

class _LineConfigKwargs(TypedDict):
    sync_mode: SYNC_MODES
    backup_dir: str | Path

_config_kwargs: _BaseConfigKwargs = {
    'read_only': False,
    'strict_schema': True,
    'sync_mode': 'data',
    'isolate_commits': True,
    'backup_dir': BACKUP_DIR
}

_line_config_kwargs: _LineConfigKwargs = {
    'sync_mode': 'data',
    'backup_dir': BACKUP_DIR
}

cfg = Config(path=CONFIG_PATH, indent=4, **_config_kwargs)
lang_cfg = Config(path=LANG_PATH, indent=4, read_only=True, read_only_jsonc=True, strict_schema=True)
runtime_cfg = cast(ConfigLike, cfg)
runtime_lang_cfg = cast(ConfigLike, lang_cfg)
log_cfg = LinesConfig(path=LOG_PATH, **_line_config_kwargs)
audit_cfg = LinesConfig(path=AUDIT_PATH, **_line_config_kwargs)

db = Database(path=DB_PATH, backup_dir=BACKUP_DIR)
# ------------------------------------------------------------
# Panels
# ------------------------------------------------------------
panels, wl = _build_panels(cfg)

if not panels and wl is None:
    log.critical("No panels initialized. Cannot start.")
    sys.exit(1)

# ------------------------------------------------------------
# Create core classes
# ------------------------------------------------------------
sub      = Subscription(
               cfg=runtime_cfg, db=db, lang_cfg=runtime_lang_cfg, audit_cfg=audit_cfg,
               app=app, panels=panels, whitelist_panel=wl
           )
bw       = BWatch(cfg=runtime_cfg, db=db, sub=sub)
api      = Api(app=app, cfg=runtime_cfg, audit_cfg=audit_cfg, sub=sub, bw=bw)
webapi   = WebApi(app=app, cfg=runtime_cfg, sub=sub, bw=bw)
adminbot = AdminBot(sub=sub, cfg=runtime_cfg, lang_cfg=runtime_lang_cfg)
bot      = PublicBot(sub=sub, cfg=runtime_cfg, lang_cfg=runtime_lang_cfg)

bw.bot   = bot  # can't do in BWatch.__init__ because PublicBot needs sub first
bw.admin_bot = adminbot

# ------------------------------------------------------------
# Broadcast selected loggers to AdminBot and jsonl logfile
# ------------------------------------------------------------
for l in (
    log, 
    
    sub.log, bw.log, 
    
    api.log, webapi.log,
    
    adminbot.log, bot.log
):
    l.set_tg_bot(adminbot)
    l.set_jsonl_handler(log_cfg)

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
# Version & GIL checks
# ------------------------------------------------------------
if sys._is_gil_enabled(): # pyright: ignore[reportPrivateUsage]
    log.warning("Free-threading disabled. Use a free-threading build for better performance.")
else:
    log.info("Free-threading active!")
if sys.version_info < (3, 14):
    log.warning(f"Use python >= 3.14 to prevent bugs. (found: {sys.version_info[0]}.{sys.version_info[1]})")

# ------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------
def _shutdown() -> None:
    log.info("Shutting down...")

    def _do_cleanup() -> None:
        if _is_primary:
            bw.stop()
            adminbot.stop()
            bot.stop()
        for panel in panels:
            panel.close()
        if wl:
            wl.close()
        db.close()
        log.info("Shutdown complete.")

    _do_cleanup()
atexit.register(_shutdown)

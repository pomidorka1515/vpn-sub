from __future__ import annotations

import fcntl
import json
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
from db import Database, DatabaseError, migrate_legacy
from loggers import Logger

from flask import Flask
from typing import cast, TypedDict
from custom_types import ConfigLike
from collections.abc import Mapping


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

# Import legacy mutable JSON state before strict config loading. The importer
# is idempotent and leaves timestamped source backups in place.
def _database_path() -> str:
    """Read the optional static database path without loading Config twice."""
    environment_path = os.environ.get('SUB_DB_PATH')
    if environment_path:
        return environment_path
    try:
        with open('../config.json', encoding='utf-8') as handle:
            raw: object = json.load(handle)
        raw_config = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else None
        configured_path = raw_config.get('database_path') if raw_config is not None else None
        if isinstance(configured_path, str) and configured_path:
            return configured_path
    except (OSError, json.JSONDecodeError):
        pass
    return '../state.sqlite3'


_db_path = _database_path()
try:
    _migration = migrate_legacy(
        db_path=_db_path,
        config_path='../config.json',
        bandwidth_path='../bw_history.json',
        snapshots_path='../snaps.json',
        backup_dir='../backup/migration',
    )
    if not _migration.already_migrated:
        log.info(f"SQLite migration imported {_migration.users} users and {_migration.codes} codes")
        if _migration.skipped_orphans:
            log.warning(f"SQLite migration skipped {_migration.skipped_orphans} orphaned or duplicate legacy records")
except DatabaseError:
    log.critical("SQLite migration failed; refusing to start with mixed JSON/SQLite state.")
    raise

# ------------------------------------------------------------
# Configs (two separate files)
# ------------------------------------------------------------
class _BaseConfigKwargs(TypedDict):
    read_only: bool
    strict_schema: bool
    sync_mode: SYNC_MODES
    isolate_commits: bool
    backup_dir: str


class _LineConfigKwargs(TypedDict):
    sync_mode: SYNC_MODES
    backup_dir: str

_config_kwargs: _BaseConfigKwargs = {
    'read_only': False,
    'strict_schema': True,
    'sync_mode': 'data',
    'isolate_commits': True,
    'backup_dir': './backup/',
}

_line_config_kwargs: _LineConfigKwargs = {
    'sync_mode': 'data',
    'backup_dir': './backup/'
}
cfg = Config(path='../config.json', indent=4, **_config_kwargs)
runtime_cfg = cast(ConfigLike, cfg)
db = Database(_db_path)
line_cfg = LinesConfig(path='../log.jsonl', **_line_config_kwargs)
audit_cfg = LinesConfig(path='../audit.jsonl', **_line_config_kwargs)
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
sub      = Subscription(
               cfg=runtime_cfg, db=db, audit_cfg=audit_cfg,
               app=app, panels=panels, whitelist_panel=wl
           )
bw       = BWatch(cfg=runtime_cfg, db=db, sub=sub)
api      = Api(app=app, cfg=runtime_cfg, audit_cfg=audit_cfg, sub=sub, bw=bw)
webapi   = WebApi(app=app, cfg=runtime_cfg, sub=sub, bw=bw)
adminbot = AdminBot(sub=sub, cfg=runtime_cfg)
bot      = PublicBot(sub=sub, cfg=runtime_cfg)

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
    l.set_jsonl_handler(line_cfg)

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

    # fork cleanup into a background thread so systemctl sees exit immediately
    t = threading.Thread(target=_do_cleanup, name='cleanup', daemon=True)
    t.start()
atexit.register(_shutdown)


if __name__ == '__main__':  # Dev only; gunicorn doesnt gaf
    app.run(port=5550)    

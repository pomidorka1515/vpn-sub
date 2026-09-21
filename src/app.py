from __future__ import annotations

import fcntl
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Self, cast

from api import Api, WebApi
from bots import AdminBot, PublicBot
from bwatch import BWatch
from config import Config, LinesConfig
from core import Subscription
from db import Database
from errors import AppError
from flask import Flask, Response, request
from loggers import Logger
from config import ConfigLike
from session import XUiSession, XUiPanelTransport
from util import err
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

threading.main_thread().name = "main"

log = Logger("app")

type PanelTransportFactory = Callable[[], XUiPanelTransport]

__all__ = [
    "AppOptions",
    "AppPaths",
    "Application",
    "PanelTransportFactory",
    "create_application",
]


class _ProxyGuard:
    """Rejects requests that did not arrive from loopback.

    The reverse proxy, healthchecks and local tooling all connect via lo,
    and gunicorn binds 127.0.0.1 so off-box traffic should be impossible -
    this is defense-in-depth for an accidental 0.0.0.0 bind. Must wrap the
    app *outside* ProxyFix, which already rewrites REMOTE_ADDR.
    """

    def __init__(self, wsgi: Any) -> None:
        self.wsgi = wsgi

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        peer = environ.get("REMOTE_ADDR")
        if peer not in ("127.0.0.1", "::1"):
            log.error("direct access attempt from %s; refusing", peer)
            body = b'{"success": false, "msg": "direct access forbidden", "obj": null}'
            start_response("400 Bad Request", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
            return [body]
        return self.wsgi(environ, start_response)


@dataclass(frozen=True, slots=True, kw_only=True)
class AppPaths:
    data: Path
    backups: Path
    config: Path
    language: Path
    database: Path
    log: Path
    audit: Path
    primary_lock: Path

    @classmethod
    def from_env(cls) -> AppPaths:
        root = Path(__file__).resolve().parent.parent
        data = Path(os.getenv("DIR_DATA", root / "data"))
        return cls(
            data=data,
            backups=Path(os.getenv("DIR_BACKUPS", data / "backup")),
            config=Path(os.getenv("PATH_CONFIG", data / "config.json")),
            language=Path(os.getenv("PATH_LANG", root / "lang.jsonc")),
            database=Path(os.getenv("PATH_DB", data / "state.sqlite3")),
            log=Path(os.getenv("PATH_LOG", data / "log.jsonl")),
            audit=Path(os.getenv("PATH_AUDIT", data / "audit.jsonl")),
            primary_lock=data / ".primary.lock",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AppOptions:
    start_background: bool = True
    start_bots: bool = True
    proxy_hops: int = 1
    require_proxy: bool = False
    panel_transport_factory: PanelTransportFactory | None = None


@dataclass(slots=True, kw_only=True)
class Application:
    app: Flask
    cfg: Config
    lang_cfg: Config
    log_cfg: LinesConfig
    audit_cfg: LinesConfig
    db: Database
    panels: list[XUiSession]
    whitelist_panel: XUiSession | None
    subscription: Subscription
    bandwidth_watcher: BWatch
    admin_bot: AdminBot
    public_bot: PublicBot
    primary: bool

    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _primary_lock_file: BinaryIO | None = None
    _start_background: bool = field(default=True, init=False)
    _start_bots: bool = field(default=True, init=False)
    _started: bool = field(default=False, init=False)

    def start(self) -> None:
        if (
            not self.primary
            or not self._start_background
            or self._started
            or self._stop_event.is_set()
        ):
            return
        self._started = True

        self.subscription.business_code_svc.recover_rollback_failures()
        self.bandwidth_watcher.start()
        if self._start_bots:
            self.admin_bot.start()
            self.public_bot.start()

        
        match sys.version_info[:2]:
            case (3, minor) if minor >= 13:
                pass
            case (3, 12):
                log.warning("This app was built for Python 3.13+, consider switching to avoid bugs (found: 3.12)")
            case (3, minor):
                raise RuntimeError(f"Error: Python 3.12+ required (detected 3.{minor})")
            case _:
                raise RuntimeError(f"Error: Python 3.12+ required (detected {sys.version})")
        
        # actually way safer than a direct call
        if getattr(sys, "_is_gil_enabled", lambda: True)():
            log.warning("Free-threading disabled. Use a free-threading build for better performance.")
        else:
            log.info("Free-threading active!")

        log.info("Launch successful!")
    
    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()

        if self.primary:
            shutdown_threads = tuple(
                threading.Thread(
                    target=component.stop,
                    name=f"{type(component).__name__} shutdown",
                    daemon=True,
                )
                for component in (
                    self.bandwidth_watcher,
                    self.admin_bot,
                    self.public_bot,
                )
            )
            for thread in shutdown_threads:
                thread.start()
            for thread in shutdown_threads:
                thread.join(timeout=6)

        self._close_all()

    def _close_all(self) -> None:
        resources: tuple[object, ...] = (
            *self.panels,
            self.whitelist_panel,
            self.db,
            self.cfg,
            self.lang_cfg,
            self.log_cfg,
            self.audit_cfg,
        )
        for resource in resources:
            if resource is None:
                continue
            close = cast(Callable[[], None], getattr(resource, "close", None))
            try:
                close()
            except Exception:
                log.error("resource cleanup failed", exc_info=True)
        if self._primary_lock_file is not None:
            self._primary_lock_file.close()
            self._primary_lock_file = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()


def _build_flask_app(options: AppOptions) -> Flask:
    flask_app = Flask(__name__)
    # should not be changed, 64KB is also plenty
    flask_app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    flask_app.config["JSON_SORT_KEYS"] = False
    if options.proxy_hops > 0:
        flask_app.wsgi_app = cast(  # type: ignore[method-assign]
            Any,
            ProxyFix(
                flask_app.wsgi_app,
                x_for=options.proxy_hops,
                x_proto=1,
                x_host=1,
            ),
        )
    if options.require_proxy:
        flask_app.wsgi_app = cast(  # type: ignore[method-assign]
            Any,
            _ProxyGuard(flask_app.wsgi_app),
        )

    @flask_app.errorhandler(AppError)
    def handle_app_error(error: AppError) -> tuple[Response, int]:  # pyright: ignore[reportUnusedFunction] -> tuple[Response, int]:
        return err(msg=error.message, code=error.status)


    @flask_app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Response, int]:  # pyright: ignore[reportUnusedFunction] -> tuple[Response, int]:
        return err(msg=error.description, code=error.code or 500)


    @flask_app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> tuple[Response, int]:  # pyright: ignore[reportUnusedFunction] -> tuple[Response, int]:
        log.error(
            "unhandled error on %s %s",
            request.method,
            request.path,
            exc_info=error,
        )
        return err(msg="Internal server error", code=500)

    return flask_app


def _acquire_primary_lock(path: Path) -> tuple[bool, BinaryIO | None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False, None
    log.info("I am the primary worker (pid=%d)", os.getpid())
    return True, handle


def _build_configs(paths: AppPaths) -> tuple[
    Config, # main 
    Config, # lang
    LinesConfig, # log
    LinesConfig # audit
]:
    paths.data.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        path=paths.config,
        indent=4,
        read_only=False,
        strict_schema=True,
        sync_mode="data",
        isolate_commits=True,
        backup_dir=paths.backups,
    )
    lang_cfg = Config(
        path=paths.language,
        indent=4,
        read_only=True,
        read_only_jsonc=True,
        strict_schema=True,
    )
    log_cfg = LinesConfig(path=paths.log, sync_mode="data", backup_dir=paths.backups)
    audit_cfg = LinesConfig(path=paths.audit, sync_mode="data", backup_dir=paths.backups)
    return cfg, lang_cfg, log_cfg, audit_cfg


def _build_panels(
    cfg: Config,
    *,
    transport_factory: PanelTransportFactory | None,
) -> tuple[list[XUiSession], XUiSession | None]:
    panels: list[XUiSession] = []
    whitelist: XUiSession | None = None

    for name, panel_cfg in cfg["3xui"].items():
        transport = transport_factory() if transport_factory is not None else None
        session = XUiSession(
            name=panel_cfg["name"],
            address=panel_cfg["address"],
            port=panel_cfg["port"],
            uri=panel_cfg["uri"],
            username=panel_cfg["username"],
            password=panel_cfg["password"],
            https=panel_cfg["https"],
            nginx_auth=tuple(panel_cfg.get("nginx_auth", [])) or None,
            inbounds_list=tuple(panel_cfg["inbounds_list"]),
            mode=panel_cfg["mode"],
            inject_headers=panel_cfg.get("inject_headers"),
            transport=transport,
        )
        if panel_cfg["whitelist"]:
            if whitelist is not None:
                log.warning("multiple whitelist panels configured; using last one (%s)", name)
            whitelist = session
        else:
            panels.append(session)

    return panels, whitelist


def _wire_loggers(
    runtime: Application,
    api: Api,
    webapi: WebApi,
) -> None:
    for logger in (
        log,
        runtime.subscription.res.log,
        runtime.bandwidth_watcher.log,
        api.log,
        webapi.log,
        runtime.admin_bot.log,
        runtime.public_bot.log,
        runtime.cfg.log,
        runtime.lang_cfg.log,
        runtime.log_cfg.log,
        runtime.audit_cfg.log,
        runtime.db.log,
    ):
        logger.set_tg_bot(runtime.admin_bot)
        logger.set_jsonl_handler(runtime.log_cfg)


def create_application(
    paths: AppPaths | None = None,
    options: AppOptions | None = None,
) -> Application:
    paths = paths or AppPaths.from_env()
    options = options or AppOptions()

    flask_app = _build_flask_app(options)
    cfg, lang_cfg, log_cfg, audit_cfg = _build_configs(paths)
    runtime_cfg = cast(ConfigLike, cfg)
    runtime_lang_cfg = cast(ConfigLike, lang_cfg)

    primary = False
    lock_file: BinaryIO | None = None
    db: Database | None = None
    panels: list[XUiSession] = []
    whitelist: XUiSession | None = None
    subscription: Subscription | None = None
    admin_bot: AdminBot | None = None
    public_bot: PublicBot | None = None
    bandwidth_watcher: BWatch | None = None

    def close_created() -> None:
        for component in (bandwidth_watcher, admin_bot, public_bot):
            if component is not None:
                try:
                    component.stop()
                except Exception:
                    log.error("startup component cleanup failed", exc_info=True)
        resources: tuple[object, ...] = ( # arbitrary length due to panels
            *panels,
            whitelist,
            db,
            cfg,
            lang_cfg,
            log_cfg,
            audit_cfg,
        )
        for resource in resources:
            if resource is None:
                continue
            close = cast(Callable[[], None], getattr(resource, "close", None))
            if close is not None:
                try:
                    close()
                except Exception:
                    log.error("startup cleanup failed", exc_info=True)
        if lock_file is not None:
            lock_file.close()

    try:
        primary, lock_file = _acquire_primary_lock(paths.primary_lock)
        db = Database(path=paths.database, backup_dir=paths.backups)
        panels, whitelist = _build_panels(
            cfg,
            transport_factory=options.panel_transport_factory,
        )
        if not panels and whitelist is None:
            raise RuntimeError("No panels initialized")

        subscription = Subscription(
            cfg=runtime_cfg,
            db=db,
            lang_cfg=runtime_lang_cfg,
            audit_cfg=audit_cfg,
            app=flask_app,
            panels=panels,
            whitelist_panel=whitelist,
        )
        admin_bot = AdminBot(sub=subscription, cfg=runtime_cfg, lang_cfg=runtime_lang_cfg)
        public_bot = PublicBot(sub=subscription, cfg=runtime_cfg, lang_cfg=runtime_lang_cfg)
        bandwidth_watcher = BWatch(
            cfg=runtime_cfg,
            db=db,
            sub=subscription,
            bot=public_bot,
            admin_bot=admin_bot,
        )
        api = Api(
            app=flask_app,
            cfg=runtime_cfg,
            audit_cfg=audit_cfg,
            sub=subscription,
            bw=bandwidth_watcher,
        )
        webapi = WebApi(app=flask_app, cfg=runtime_cfg, sub=subscription, bw=bandwidth_watcher)

        runtime = Application(
            app=flask_app,
            cfg=cfg,
            lang_cfg=lang_cfg,
            log_cfg=log_cfg,
            audit_cfg=audit_cfg,
            db=db,
            panels=panels,
            whitelist_panel=whitelist,
            subscription=subscription,
            bandwidth_watcher=bandwidth_watcher,
            admin_bot=admin_bot,
            public_bot=public_bot,
            primary=primary,
        )
        runtime._primary_lock_file = lock_file  # pyright: ignore[reportPrivateUsage]
        runtime._start_background = options.start_background  # pyright: ignore[reportPrivateUsage]
        runtime._start_bots = options.start_bots  # pyright: ignore[reportPrivateUsage]
        _wire_loggers(runtime, api, webapi)
        if options.start_background:
            runtime.start()
        return runtime
    except Exception:
        close_created()
        raise

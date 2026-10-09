from __future__ import annotations

import fcntl
import os
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Self, cast

from api import Api, WebApi
from api.common import RES_DIR
from api.decorators.rate_limit import close_rate_limit, configure_rate_limit_from_config
from bots import AdminBot, PublicBot
from bwatch import BWatch
from config import AppConfig, Config, LangConfig, LinesConfig
from core import Subscription
from db import Database
from errors import AppError
from flask import Flask, Response, request
from jinja2 import FileSystemLoader
from loggers import Logger, Colors
from paths import bundled_root, compiled, program_dir, runtime_dir
from session import XUiSession, XUiPanelTransport
from util import err
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from wsgiref.types import WSGIApplication, StartResponse, WSGIEnvironment
from flask.json.provider import DefaultJSONProvider

threading.main_thread().name = "main"

log = Logger("app")

type PanelTransportFactory = Callable[[], XUiPanelTransport]

__all__ = [
    "AppOptions",
    "AppPaths",
    "Application",
    "PanelTransportFactory",
    "create_application",
    "verbose_overrides",
]


def verbose_overrides(raw: str | None = None) -> frozenset[str]:
    """Names in ``VERBOSE_LOGGING_OVERRIDES`` that should start verbose.

    Comma-separated, case-insensitive. Empty tokens are dropped, so
    ``bwatch,``, ``bwatch``, and ``,`` all parse. Unknown names are ignored.
    Unset is quiet.
    """
    if raw is None:
        raw = os.getenv("VERBOSE_LOGGING_OVERRIDES")
    if raw is None:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


class _OrderedJSONProvider(DefaultJSONProvider):
    """Keep object key order. Flask 3 ignores JSON_SORT_KEYS."""

    sort_keys = False


class _ProxyGuard:
    """Rejects requests that did not arrive from loopback.

    The reverse proxy, healthchecks and local tooling all connect via lo,
    and gunicorn binds 127.0.0.1 so off-box traffic should be impossible -
    this is defense-in-depth for an accidental 0.0.0.0 bind. Must wrap the
    app *outside* ProxyFix, which already rewrites REMOTE_ADDR.
    """

    def __init__(self, wsgi: WSGIApplication) -> None:
        self.wsgi: WSGIApplication = wsgi

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
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
        shipped = bundled_root()
        data = Path(os.getenv("DIR_DATA", program_dir() / "data"))
        return cls(
            data=data,
            backups=Path(os.getenv("DIR_BACKUPS", data / "backup")),
            config=Path(os.getenv("PATH_CONFIG", data / "config.json")),
            language=Path(os.getenv("PATH_LANG", shipped / "lang.jsonc")),
            database=Path(os.getenv("PATH_DB", data / "state.sqlite3")),
            log=Path(os.getenv("PATH_LOG", data / "log.jsonl")),
            audit=Path(os.getenv("PATH_AUDIT", data / "audit.jsonl")),
            primary_lock=runtime_dir(data) / ".primary.lock",
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
    cfg: Config[AppConfig]
    lang_cfg: Config[LangConfig]
    log_cfg: LinesConfig
    audit_cfg: LinesConfig
    db: Database
    panels: list[XUiSession]
    whitelist_panel: XUiSession | None
    subscription: Subscription
    bandwidth_watcher: BWatch
    admin_bot: AdminBot | None
    public_bot: PublicBot | None
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
            if self.admin_bot is not None:
                self.admin_bot.start()
            if self.public_bot is not None:
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
        
        log.info(f"{Colors.BOLD}Launch successful!{Colors.RESET}")
    
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
                if component is not None
            )
            for thread in shutdown_threads:
                thread.start()
            for thread in shutdown_threads:
                thread.join(timeout=6)

        self._close_all()
        close_rate_limit()

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
    flask_app.json_provider_class = _OrderedJSONProvider
    flask_app.json = _OrderedJSONProvider(flask_app)
    # pages live in res/, not a flask-style templates/ directory
    # jinja_loader is a cached_property; assigning replaces the template lookup.
    cast(Any, flask_app).jinja_loader = FileSystemLoader(str(RES_DIR))
    # 1 MiB: the config editor posts whole xray profile objects. Werkzeug
    # applies this before the view, so it cannot be per-route.
    flask_app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    if options.proxy_hops > 0:
        flask_app.wsgi_app = ProxyFix( # type: ignore[method-assign]
            flask_app.wsgi_app,
            x_for=options.proxy_hops,
            x_proto=1,
            x_host=1
        )
    if options.require_proxy:
        flask_app.wsgi_app = _ProxyGuard(flask_app.wsgi_app) # type: ignore[method-assign]


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


def _build_configs(paths: AppPaths, *, start_backup: bool) -> tuple[
    Config[AppConfig], # main
    Config[LangConfig], # lang
    LinesConfig, # log
    LinesConfig # audit
]:
    paths.data.mkdir(parents=True, exist_ok=True)
    lock_dir = paths.primary_lock.parent
    cfg = Config[AppConfig](
        path=paths.config,
        indent=4,
        read_only=False,
        strict_schema=True,
        schema_path=bundled_root() / "config.schema.json" if compiled() else None,
        sync_mode="data",
        isolate_commits=True,
        backup_dir=paths.backups,
        lockfile_path=lock_dir,
        start_backup=start_backup,
    )
    lang_cfg = Config[LangConfig](
        path=paths.language,
        indent=4,
        read_only=True,
        read_only_jsonc=True,
        strict_schema=True,
        lockfile_path=lock_dir,
    )
    log_cfg = LinesConfig(
        path=paths.log,
        sync_mode="data",
        backup_dir=paths.backups,
        lockfile_path=lock_dir,
        start_backup=start_backup,
    )
    audit_cfg = LinesConfig(
        path=paths.audit,
        sync_mode="data",
        backup_dir=paths.backups,
        lockfile_path=lock_dir,
        start_backup=start_backup,
    )
    return cfg, lang_cfg, log_cfg, audit_cfg


def _build_panels(
    cfg: Config[AppConfig],
    *,
    stamp_dir: Path,
    transport_factory: PanelTransportFactory | None,
    verbose: bool = False,
) -> tuple[list[XUiSession], XUiSession | None]:
    panels: list[XUiSession] = []
    whitelist: XUiSession | None = None

    conf = cfg.view()
    for name, panel_cfg in conf["3xui"].items():
        transport = transport_factory() if transport_factory is not None else None
        session = XUiSession(
            name=panel_cfg["name"],
            address=panel_cfg["address"],
            port=panel_cfg["port"],
            uri=panel_cfg["uri"],
            api_token=panel_cfg["token"],
            https=panel_cfg["https"],
            # The schema validates this array's length of two.
            nginx_auth=cast(tuple[str, str] | None, tuple(panel_cfg.get("nginx_auth", [])) or None),
            inbounds_list=tuple(panel_cfg["inbounds_list"]),
            mode=panel_cfg["mode"],
            # The schema guarantees only an object; preserve the session boundary.
            inject_headers=cast(Mapping[str, str | bytes] | None, panel_cfg.get("inject_headers")),
            transport=transport,
            stamp_dir=str(stamp_dir),
            verbose=verbose,
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
    loggers: list[Logger] = [
        log,
        runtime.subscription.res.log,
        runtime.bandwidth_watcher.log,
        api.log,
        webapi.log,
        runtime.cfg.log,
        runtime.lang_cfg.log,
        runtime.log_cfg.log,
        runtime.audit_cfg.log,
        runtime.db.log,
    ]
    if runtime.admin_bot is not None:
        loggers.append(runtime.admin_bot.log)
    if runtime.public_bot is not None:
        loggers.append(runtime.public_bot.log)
    for logger in loggers:
        if runtime.admin_bot is not None:
            logger.set_tg_bot(runtime.admin_bot)
        logger.set_jsonl_handler(runtime.log_cfg)


def _nonempty_token(section: object) -> bool:
    if not isinstance(section, dict):
        return False
    token = cast(dict[str, object], section).get("token")
    return isinstance(token, str) and token != ""


def create_application(
    paths: AppPaths | None = None,
    options: AppOptions | None = None,
) -> Application:
    paths = paths or AppPaths.from_env()
    options = options or AppOptions()

    flask_app = _build_flask_app(options)
    primary = False
    lock_file: BinaryIO | None = None
    cfg: Config[AppConfig] | None = None
    lang_cfg: Config[LangConfig] | None = None
    log_cfg: LinesConfig | None = None
    audit_cfg: LinesConfig | None = None

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
        close_rate_limit()

    try:
        primary, lock_file = _acquire_primary_lock(paths.primary_lock)
        cfg, lang_cfg, log_cfg, audit_cfg = _build_configs(paths, start_backup=primary)
        configure_rate_limit_from_config(cfg)
        db = Database(
            path=paths.database,
            backup_dir=paths.backups,
            start_backup=primary,
        )
        overrides = verbose_overrides()
        panels, whitelist = _build_panels(
            cfg,
            stamp_dir=paths.primary_lock.parent,
            transport_factory=options.panel_transport_factory,
            verbose="panels" in overrides,
        )
        if not panels and whitelist is None:
            raise RuntimeError("No panels initialized")

        subscription = Subscription(
            cfg=cfg,
            db=db,
            lang_cfg=lang_cfg,
            audit_cfg=audit_cfg,
            app=flask_app,
            panels=panels,
            whitelist_panel=whitelist,
            verbose="core" in overrides,
        )
        conf = cfg.view()
        admin_bot = (
            AdminBot(sub=subscription, cfg=cfg, lang_cfg=lang_cfg)
            if _nonempty_token(conf.get("bot"))
            else None
        )
        public_bot = (
            PublicBot(sub=subscription, cfg=cfg, lang_cfg=lang_cfg)
            if _nonempty_token(conf.get("publicbot"))
            else None
        )
        bandwidth_watcher = BWatch(
            cfg=cfg,
            db=db,
            sub=subscription,
            bot=public_bot,
            admin_bot=admin_bot,
            verbose="bwatch" in overrides,
        )
        api = Api(
            app=flask_app,
                cfg=cfg,
            audit_cfg=audit_cfg,
            sub=subscription,
            bw=bandwidth_watcher,
        )
        webapi = WebApi(app=flask_app, cfg=cfg, sub=subscription, bw=bandwidth_watcher)

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

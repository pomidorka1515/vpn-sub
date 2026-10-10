from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from errors import DatabaseError
from loggers import Logger

from .backup import do_backup, instance_backup_dir, make_backup_thread, prune_backups
from .codes import CodesMixin
from .common import row_dict
from .schema import SchemaMixin
from .state import StateMixin
from .telegram import TelegramMixin
from .users import UsersMixin

if TYPE_CHECKING:
    from collections.abc import Generator


class Database(UsersMixin, CodesMixin, TelegramMixin, StateMixin, SchemaMixin):
    """Thread-safe SQLite repository for dynamic application state."""

    def __init__(
        self,
        *,
        path: str | Path,
        timeout: float = 5.0,
        backup_dir: str | Path | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3,
        start_backup: bool = True,
    ) -> None:
        """
        Args:
            path: Path to the SQLite database.
            timeout: Connection timeout in seconds.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for the backups.
            backup_retention: Amount of concurrent backups kept on disk.
            start_backup: Start the scheduled backup thread. False keeps
                backup_dir configured so backup_now() still works.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.path = os.path.normpath(Path(path).absolute())
            self.timeout = timeout
            self._connections: dict[int, sqlite3.Connection] = {}
            self._connections_lock = threading.Lock()
            self._local = threading.local()
            self._backup_dir: str | None = str(backup_dir) if backup_dir else None
            self._backup_interval: int | float = backup_interval
            self._backup_retention: int = backup_retention
            self._backup_stop = threading.Event()
            self._backup_t: threading.Thread | None = None
            parent = Path(self.path).parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(f"unable to create database directory {parent}: {exc}") from exc
            self.initialize()
            if self._backup_dir and start_backup:
                self._backup_t = make_backup_thread(
                    path=self.path,
                    timeout=self.timeout,
                    backup_dir=self._backup_dir,
                    backup_interval=self._backup_interval,
                    backup_retention=self._backup_retention,
                    stop_event=self._backup_stop,
                )
                self._backup_t.start()

    def _thread_id(self) -> int:
        return threading.get_ident()

    def _cached(self) -> sqlite3.Connection | None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        opener = getattr(self._local, "opener", None)
        if conn is None:
            return None
        # Tests replace ``_connect`` with a plain function. A bound method is
        # a new object on every access, so compare the underlying function.
        if opener is not self._opener():
            self._discard(conn)
            return None
        return conn

    def _opener(self) -> object:
        connect = self._connect
        return getattr(connect, "__func__", connect)

    def _discard(self, conn: sqlite3.Connection) -> None:
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
            self._local.opener = None
        with self._connections_lock:
            current = self._connections.get(self._thread_id())
            if current is conn:
                del self._connections[self._thread_id()]
        with suppress(sqlite3.Error):
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {max(1, int(self.timeout * 1000))}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            with self._connections_lock:
                previous = self._connections.get(self._thread_id())
                self._connections[self._thread_id()] = conn
            if previous is not None and previous is not conn:
                with suppress(sqlite3.Error):
                    previous.close()
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise DatabaseError(f"unable to open database {self.path}: {exc}") from exc

    def _connection(self) -> sqlite3.Connection:
        cached = self._cached()
        if cached is not None:
            return cached
        conn = self._connect()
        self._local.conn = conn
        self._local.opener = self._opener()
        return conn

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        conn = self._connection()
        try:
            yield conn
        except sqlite3.Error as exc:
            if conn.in_transaction or self._dead(exc):
                self._discard(conn)
            raise DatabaseError(str(exc)) from exc

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[sqlite3.Connection]:
        with self.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if conn.in_transaction or self._dead(exc):
                    self._discard(conn)
                raise DatabaseError(str(exc)) from exc
            except Exception:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if conn.in_transaction:
                    self._discard(conn)
                raise

    @staticmethod
    def _dead(exc: sqlite3.Error) -> bool:
        if isinstance(exc, sqlite3.ProgrammingError):
            return True
        message = str(exc).lower()
        return "closed" in message or "disk i/o" in message

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, object] | None:
        return row_dict(row)

    @property
    def backup_dir(self) -> str | None:
        return self._backup_dir

    @property
    def backup_interval(self) -> int | float:
        return self._backup_interval

    @property
    def backup_retention(self) -> int:
        return self._backup_retention

    def backup_now(self) -> None:
        """Trigger an immediate backup snapshot, regardless of the schedule.

        Obeys the same retention limit as the scheduled backup thread.

        Raises:
            DatabaseError: If no backup_dir was configured on this instance.
        """
        if self._backup_dir is None:
            raise DatabaseError("backup_now() requires a backup_dir to be configured.")
        instance_dir = instance_backup_dir(self.path, self._backup_dir)
        do_backup(self.path, self.timeout, instance_dir, self.log)
        prune_backups(instance_dir, self._backup_retention, self.log)

    def close(self) -> None:
        self._backup_stop.set()
        if self._backup_t is not None:
            self._backup_t.join(timeout=5)
        with self._connections_lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        self._local.conn = None
        self._local.opener = None
        for conn in connections:
            with suppress(sqlite3.Error):
                conn.close()

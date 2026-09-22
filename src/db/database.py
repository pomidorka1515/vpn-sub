from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path

from errors import DatabaseError
from loggers import Logger

from .backup import do_backup, instance_backup_dir, make_backup_thread, prune_backups
from .common import row_dict
from .codes import CodesMixin
from .schema import SchemaMixin
from .state import StateMixin
from .telegram import TelegramMixin
from .users import UsersMixin


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
    ) -> None:
        """
        Args:
            path: Path to the SQLite database.
            timeout: Connection timeout in seconds.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for the backups.
            backup_retention: Amount of concurrent backups kept on disk.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.path = os.path.abspath(path)
            self.timeout = timeout
            self._connections: set[sqlite3.Connection] = set()
            self._connections_lock = threading.Lock()
            self._backup_dir: str | None = str(backup_dir) if backup_dir else None
            self._backup_interval: int | float = backup_interval
            self._backup_retention: int = backup_retention
            self._backup_stop = threading.Event()
            self._backup_t: threading.Thread | None = None
            parent = os.path.dirname(self.path)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(f"unable to create database directory {parent}: {exc}") from exc
            self.initialize()
            if self._backup_dir:
                self._backup_t = make_backup_thread(
                    path=self.path,
                    timeout=self.timeout,
                    backup_dir=self._backup_dir,
                    backup_interval=self._backup_interval,
                    backup_retention=self._backup_retention,
                    stop_event=self._backup_stop,
                )
                self._backup_t.start()

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {max(1, int(self.timeout * 1000))}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            with self._connections_lock:
                self._connections.add(conn)
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise DatabaseError(f"unable to open database {self.path}: {exc}") from exc

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        with self.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DatabaseError(str(exc)) from exc
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

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
            connections = tuple(self._connections)
            self._connections.clear()
        for conn in connections:
            conn.close()

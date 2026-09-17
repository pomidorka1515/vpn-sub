import os
import threading
import json
import tempfile
import io

from pathlib import Path
from collections.abc import Mapping, Iterator, Sequence
from types import TracebackType
from typing import Callable, Literal, Self

from ._constants import SYNC_MODES, JsonValue, JsonDict
from ._atomic import CompactReturn, _ensure_parent_dir, _locked_file, _fsync_parent_dir
from ._backup import _make_backup_thread, _instance_backup_dir, _do_backup, _prune_backups

from errors import ConfigError
from loggers import Logger

class LinesConfig:
    """
    Thread-safe, process-safe line-based config manager.

    Each line is a JSON object. The file is append-only for writes; reads
    can iterate from the beginning or tail. Suitable for JSONL/NDJSON logs.

    Notes:
    - Uses a dedicated `.lock` file for inter-process locking.
    - Sync behavior is consistent with Config (full/data/none via sync_mode).
    - Automatic backup support with configurable interval and retention.
    """

    def __init__(
        self,
        path: str | Path,
        sync_mode: SYNC_MODES = 'data',
        backup_dir: str | Path | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3,
    ) -> None:
        """
        Args:
            path: Path to the line-based file (created if missing, including
                parent directories).
            sync_mode: 'full' fsyncs file + parent directory, 'data' fsyncs file only,
                'none' skips fsync entirely.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for backups.
            backup_retention: Amount of concurrent backups kept on disk.
        """
        self.log = Logger(type(self).__name__)
        path_str = str(path)
        with self.log.loading():
            if not path_str.endswith('.jsonl'):
                self.log.warning("path doesnt end with .jsonl, did you specify the correct path?")
            self._path: str = path_str
            self._sync_mode: SYNC_MODES = sync_mode
            self._lock = threading.RLock()

            self._backup_dir: str | None = str(backup_dir) if backup_dir else None
            self._backup_interval: int | float = backup_interval
            self._backup_retention: int = backup_retention
            self._backup_stop = threading.Event()

            _ensure_parent_dir(path_str)
            if not os.path.exists(self._path):
                with open(self._path, "a", encoding="utf-8"):
                    pass
            if self._backup_dir:
                self._backup_t: threading.Thread | None  = _make_backup_thread(
                    path=self._path,
                    indent=4,
                    backup_dir=self._backup_dir,
                    backup_interval=backup_interval,
                    backup_retention=backup_retention,
                    stop_event=self._backup_stop,
                    config_type='jsonl',
                    raw=True,
                )
                self._backup_t.start()
            else:
                self._backup_t = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def size(self) -> int:
        return os.path.getsize(self.path)

    def append(self, record: Mapping[str, JsonValue]) -> None:
        """Append a JSON object as a new line. Thread-safe."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with _locked_file(self._path, exclusive=True):
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
                    if self._sync_mode != "none":
                        f.flush()
                        os.fsync(f.fileno())
                if self._sync_mode == "full":
                    _fsync_parent_dir(self._path)

    def append_many(self, records: Sequence[Mapping[str, JsonValue]]) -> None:
        """Append multiple records in a single atomic write. Thread-safe.

        Note: records must be a Sequence (list/tuple). Iterables are not supported
        because the entire collection is materialized before writing.
        """
        lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        with self._lock:
            with _locked_file(self._path, exclusive=True):
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(lines)
                    if self._sync_mode != "none":
                        f.flush()
                        os.fsync(f.fileno())
                if self._sync_mode == "full":
                    _fsync_parent_dir(self._path)

    def __iter__(self) -> Iterator[JsonDict]:
        """Iterate over all lines. Thread-safe at read-time."""
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                yield json.loads(line)
                except OSError:
                    return

    def tail(self, n: int = 100) -> Iterator[JsonDict]:
        """Iterate over the last n lines. Thread-safe at read-time."""
        raw_lines: bytes = b""
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                try:
                    with open(self._path, "rb") as f:
                        f.seek(0, io.SEEK_END)
                        remaining: int = f.tell()
                        if remaining == 0:
                            return
                        while remaining > 0:
                            chunk_size = min(8192, remaining)
                            f.seek(max(0, remaining - chunk_size))
                            chunk = f.read(chunk_size)
                            remaining -= chunk_size
                            raw_lines = chunk + raw_lines
                except OSError:
                    if not raw_lines:
                        return

        lines = raw_lines.decode("utf-8").splitlines()
        for line in lines[-n:]:
            line = line.strip()
            if line:
                yield json.loads(line)

    def read_all(self) -> list[JsonDict]:
        """Return all records as a list. Thread-safe.

        Convenience wrapper over list(self), prefer iterating directly when
        the file is large and you don't need everything in memory at once.
        """
        return list(self)

    def first(self, n: int = 1) -> list[JsonDict]:
        """Return the first n records without reading the whole file. Thread-safe.

        Stops reading as soon as n records have been collected, making it
        efficient for large files where only the oldest entries are needed.
        """
        result: list[JsonDict] = []
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                result.append(json.loads(line))
                                if len(result) >= n:
                                    break
                except FileNotFoundError:
                    pass
        return result

    def count(self) -> int:
        """Count total lines. Thread-safe at read-time."""
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        return sum(1 for line in f if line.strip())
                except OSError:
                    return 0

    def clear(self) -> None:
        """Truncate the file. Thread-safe.

        Uses 'r+' mode to acquire only the shared lock needed for truncation,
        avoiding a deadlock hazard where the lockfile's exclusive lock would
        block on a process mid-append/compact/clear that holds it.
        """
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                # 'r+' acquires a shared (advisory) lock on the data fd.
                # This is the minimum lock level needed for truncation —
                # unlike 'w' mode, it won't deadlock if another process is
                # mid-append/compact/clear and holds the lockfile's exclusive
                # lock while holding the data file's shared lock.
                with open(self._path, "r+", encoding="utf-8") as f:
                    f.truncate(0)
                    if self._sync_mode != "none":
                        f.flush()
                        os.fsync(f.fileno())
                if self._sync_mode == "full":
                    _fsync_parent_dir(self._path)

    def compact(self, keep: Callable[[Mapping[str, JsonValue]], bool] | None = None) -> CompactReturn:
        """Rewrite the file keeping only records that satisfy `keep`. Thread-safe.

        This is the primary way to delete or deduplicate records, since JSONL
        files do not support in-place line removal.

        Args:
            keep: Predicate called for every record. Records for which it returns
                True are retained. Pass None to keep all records (acts as a
                clean rewrite with no lines dropped).

        Returns:
            Tuple of (kept, removed) counts after compaction.
        """
        dir_path = os.path.dirname(self._path) or "."
        with self._lock:
            with _locked_file(self._path, exclusive=True):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        records = [json.loads(line) for line in f if line.strip()]
                except OSError:
                    return CompactReturn(0, 0)

                kept = [r for r in records if keep(r)] if keep is not None else records
                removed = len(records) - len(kept)

                fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".tmp_compact_", suffix=".jsonl")
                os.close(fd)
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        for r in kept:
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                        if self._sync_mode != "none":
                            f.flush()
                            os.fsync(f.fileno())
                    os.replace(tmp, self._path)
                    if self._sync_mode == "full":
                        _fsync_parent_dir(self._path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                    raise

        return CompactReturn(len(kept), removed)

    def backup_now(self) -> None:
        """Trigger an immediate backup snapshot, regardless of the schedule.

        Obeys the same retention limit as the scheduled backup thread.

        Raises:
            ConfigError: If no backup_dir was configured on this instance.
        """
        if self._backup_dir is None:
            raise ConfigError("backup_now() requires a backup_dir to be configured.")
        instance_dir = _instance_backup_dir(self._path, self._backup_dir)
        _do_backup(self._path, 4, instance_dir, self.log, raw=True)
        _prune_backups(instance_dir, self._backup_retention, self.log, config_type='jsonl')

    def close(self) -> None:
        """Stop backup thread. Does not affect the data file."""
        self._backup_stop.set()
        if self._backup_t is not None:
            self._backup_t.join(timeout=5)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False] | None:
        self.close()
        return False

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from typing import NamedTuple

from .constants import SYNC_MODES, JsonValue


class FileSignature(NamedTuple):
    """Unique file identifier after atomic write."""
    mtime_ns: int
    size: int
    inode: int
    device: int

class CompactReturn(NamedTuple):
    kept: int
    removed: int



def ensure_parent_dir(path: str) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

def resolve_lockfile_path(data_path: str, lockfile_path: str | None = None) -> str:
    """Resolve an inter-process lock path.

    None keeps the lock beside the data file as ``{data_path}.lock``.
    A directory (existing, or a path ending in a separator) places
    ``{basename}.{sha1(abspath)[:8]}.lock`` inside it so two files that share
    a basename do not collide. Any other path is used as the lock file.
    """
    if lockfile_path is None:
        return f"{data_path}.lock"
    if lockfile_path.endswith(("/", os.sep)) or os.path.isdir(lockfile_path):
        absolute = os.path.abspath(data_path)
        digest = hashlib.sha1(absolute.encode(), usedforsecurity=False).hexdigest()[:8]
        name = f"{os.path.basename(data_path)}.{digest}.lock"
        return os.path.join(lockfile_path, name)
    return lockfile_path

@contextmanager
def locked_file(
    path: str,
    *,
    exclusive: bool,
    lockfile_path: str | None = None,
) -> Generator[None]:
    ensure_parent_dir(path)
    resolved = lockfile_path if lockfile_path is not None else resolve_lockfile_path(path)
    ensure_parent_dir(resolved)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with open(resolved, "a+b") as lock_fp:
        fcntl.flock(lock_fp, mode)
        try:
            yield
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)

def stat_signature(path: str) -> FileSignature | None:
    try:
        stat_result = os.stat(path)
    except FileNotFoundError:
        return None
    return FileSignature(
        mtime_ns=stat_result.st_mtime_ns,
        size=stat_result.st_size,
        inode=stat_result.st_ino,
        device=stat_result.st_dev,
    )

def file_signature(path: str) -> FileSignature | None:
    return stat_signature(path)


def fsync_parent_dir(path: str) -> None:
    dir_path = os.path.dirname(path) or "."
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)



def atomic_write_json(
    path: str,
    data: Mapping[str, JsonValue],
    *,
    minify: bool,
    indent: int,
    sync_mode: SYNC_MODES,
) -> FileSignature | None:
    """Write atomically: temp file + os.replace. Returns new file signature."""
    ensure_parent_dir(path)
    dir_path = os.path.dirname(path) or "."

    fd, temp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".json")
    os.close(fd)

    try:
        if os.path.exists(path):
            existing = os.stat(path, follow_symlinks=False)
            os.chmod(temp_path, existing.st_mode & 0o777)
            with suppress(PermissionError):
                os.chown(temp_path, existing.st_uid, existing.st_gid)

        with open(temp_path, "w", encoding="utf-8") as handle:
            if minify:
                json.dump(data, handle, indent=None, separators=(',', ':'), ensure_ascii=False)
            else:
                json.dump(data, handle, indent=indent, ensure_ascii=False)
            handle.write("\n")

            if sync_mode != "none":
                handle.flush()
                os.fsync(handle.fileno())

        os.replace(temp_path, path)

        if sync_mode == "full":
            fsync_parent_dir(path)

    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temp_path)
        raise

    return file_signature(path)

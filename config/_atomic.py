import os
import fcntl
import json
import tempfile

from typing import NamedTuple
from contextlib import contextmanager
from collections.abc import Mapping, Generator

from ._constants import SYNC_MODES, JsonValue

class FileSignature(NamedTuple):
    """Unique file identifier after atomic write."""
    mtime_ns: int
    size: int
    inode: int
    device: int

class CompactReturn(NamedTuple):
    kept: int
    removed: int



def _ensure_parent_dir(path: str) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

def _lockfile_path(path: str) -> str:
    return f"{path}.lock"

@contextmanager
def _locked_file(path: str, *, exclusive: bool) -> Generator[None, None, None]:
    _ensure_parent_dir(path)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with open(_lockfile_path(path), "a+b") as lock_fp:
        fcntl.flock(lock_fp, mode)
        try:
            yield
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)

def _stat_signature(path: str) -> FileSignature | None:
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

def _file_signature(path: str) -> FileSignature | None:
    return _stat_signature(path)


def _fsync_parent_dir(path: str) -> None:
    dir_path = os.path.dirname(path) or "."
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)



def _atomic_write_json(
    path: str,
    data: Mapping[str, JsonValue],
    *,
    minify: bool,
    indent: int,
    sync_mode: SYNC_MODES,
) -> FileSignature | None:
    """Write atomically: temp file + os.replace. Returns new file signature."""
    _ensure_parent_dir(path)
    dir_path = os.path.dirname(path) or "."

    fd, temp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".json")
    os.close(fd)

    try:
        if os.path.exists(path):
            existing = os.stat(path, follow_symlinks=False)
            os.chmod(temp_path, existing.st_mode & 0o777)
            try:
                os.chown(temp_path, existing.st_uid, existing.st_gid)
            except PermissionError:
                pass

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
            _fsync_parent_dir(path)

    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise

    return _file_signature(path)


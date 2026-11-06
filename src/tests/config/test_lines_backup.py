from __future__ import annotations

import fcntl
import itertools
import shutil
import threading
from pathlib import Path
from unittest.mock import DEFAULT, Mock

import pytest

from backuputil import publish_backup
from config import LinesConfig
from config import backup as backup_module
from config.atomic import locked_file, resolve_lockfile_path


@pytest.mark.parametrize("mode", ["immediate", "scheduled", "retry"])
@pytest.mark.parametrize("custom_lock", [False, True])
def test_lines_backup_waits_for_complete_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    custom_lock: bool,
) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text('{"id": 1}\n', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    lock_path = str(lock_dir) if custom_lock else None
    resolved_lock = resolve_lockfile_path(str(path), lock_path)
    attempted = threading.Event()
    published = threading.Event()
    errors: list[Exception] = []
    real_flock = fcntl.flock
    real_copy = shutil.copy2
    real_publish = publish_backup
    real_backup = backup_module.do_backup
    retry_backup = Mock(
        wraps=real_backup,
        side_effect=itertools.chain(
            [OSError("simulated backup failure")], itertools.repeat(DEFAULT)
        ),
    )

    def observed_flock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_SH:
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                attempted.set()
            else:
                real_flock(fd, fcntl.LOCK_UN)
        real_flock(fd, operation)

    def checked_copy(src: str, dst: str) -> str:
        # The shared lock must remain held for the entire source copy.
        with Path(resolved_lock).open("a+b") as lock:
            with pytest.raises(BlockingIOError):
                real_flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = real_copy(src, dst)
            with pytest.raises(BlockingIOError):
                real_flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return result

    def observed_publish(tmp: str, directory: str, suffix: str) -> Path:
        result = real_publish(tmp, directory, suffix)
        published.set()
        return result

    monkeypatch.setattr(backup_module, "publish_backup", observed_publish)
    monkeypatch.setattr(shutil, "copy2", checked_copy)
    if mode == "retry":
        monkeypatch.setattr(backup_module, "do_backup", retry_backup)

    def immediate_backup(config: LinesConfig) -> None:
        try:
            config.backup_now()
        except Exception as exc:
            errors.append(exc)

    worker: threading.Thread | None = None
    lines: LinesConfig | None = None
    try:
        with locked_file(str(path), lockfile_path=resolved_lock, exclusive=True):
            with path.open("a", encoding="utf-8") as output:
                output.write('{"id": ')
                output.flush()
                monkeypatch.setattr(fcntl, "flock", observed_flock)
                lines = LinesConfig(
                    path,
                    sync_mode="none",
                    backup_dir=backup_dir,
                    lockfile_path=lock_path,
                    backup_interval=0.01,
                    start_backup=mode != "immediate",
                )
                if mode == "immediate":
                    worker = threading.Thread(target=immediate_backup, args=(lines,))
                    worker.start()
                assert attempted.wait(5), "backup did not contend on the append lock"
                assert not published.is_set()
                output.write('2, "text": "complete"}\n')
                output.flush()
        assert published.wait(5), "backup did not finish after releasing the append lock"
        lines.close()
        if worker is not None:
            worker.join(timeout=5)
            assert not worker.is_alive()
        assert not errors
        if mode == "retry":
            assert retry_backup.call_count >= 2
        snapshots = list((backup_dir / path.stem).glob("*.jsonl"))
        assert snapshots
        expected = [{"id": 1}, {"id": 2, "text": "complete"}]
        for snapshot in snapshots:
            assert snapshot.read_bytes().endswith(b"\n")
            restored_path = tmp_path / "restored.jsonl"
            shutil.copyfile(snapshot, restored_path)
            with LinesConfig(restored_path, sync_mode="none", start_backup=False) as restored:
                assert restored.read_all() == expected
    finally:
        if lines is not None:
            lines.close()
        if worker is not None:
            worker.join(timeout=5)

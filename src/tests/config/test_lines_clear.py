from __future__ import annotations

import fcntl
import threading
from typing import TYPE_CHECKING

from config import LinesConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_clear_waits_for_reader_from_another_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    attempted = threading.Event()
    finished = threading.Event()
    contended: list[bool] = []
    errors: list[Exception] = []
    real_flock = fcntl.flock

    def observed_flock(fd: int, operation: int) -> None:
        if operation in {fcntl.LOCK_SH, fcntl.LOCK_EX}:
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                contended.append(True)
            else:
                contended.append(False)
                real_flock(fd, fcntl.LOCK_UN)
            attempted.set()
        real_flock(fd, operation)

    with (
        LinesConfig(path, sync_mode="none") as reader,
        LinesConfig(path, sync_mode="none") as writer,
    ):
        records = iter(reader)
        assert next(records) == {"id": 1}

        def clear() -> None:
            try:
                writer.clear()
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        monkeypatch.setattr(fcntl, "flock", observed_flock)
        worker = threading.Thread(target=clear, daemon=True)
        try:
            worker.start()
            assert attempted.wait(5), "clear did not attempt the lock"
            assert contended == [True], "clear did not block on the reader's shared lock"
            assert not finished.is_set()
            assert path.read_text(encoding="utf-8") == '{"id": 1}\n{"id": 2}\n'
        finally:
            list(records)
            worker.join(timeout=5)
        assert not worker.is_alive(), "clear did not finish after the reader released its lock"
        assert not errors
        assert finished.is_set()
        assert path.read_text(encoding="utf-8") == ""

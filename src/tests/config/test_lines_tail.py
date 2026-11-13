from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO, cast
from unittest.mock import MagicMock, patch

import pytest

from config import LinesConfig


@pytest.mark.parametrize(
    ("content", "n", "expected"),
    [
        ("", 1, []),
        ('{"id": 1}\n{"id": 2}\n', 1, [{"id": 2}]),
        ('{"id": 1}\n{"id": 2}', 1, [{"id": 2}]),
        ('{"id": 1}\n\n{"id": 2}\n \n', 2, [{"id": 2}]),
        ('{"id": 1}\n\n', 1, []),
        ('{"id": 1}\r\n{"id": 2}\r\n', 5, [{"id": 1}, {"id": 2}]),
        ('{"id": 1}\n{"id": 2}', 0, []),
        ('{"id": 1}\n{"id": 2}', -1, [{"id": 2}]),
    ],
)
def test_tail_line_selection(
    tmp_path: Path,
    content: str,
    n: int,
    expected: list[dict[str, int]],
) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(content, encoding="utf-8")
    with LinesConfig(path, sync_mode="none") as lines:
        assert list(lines.tail(n)) == expected


def test_tail_reads_only_a_small_suffix(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"id": 1}\n' * 100_000 + b'{"id": 2}\n')
    bytes_read = 0
    original_open = Path.open

    def tracked_open(file_path: Path, mode: str) -> BinaryIO:
        nonlocal bytes_read
        handle = cast(BinaryIO, original_open(file_path, mode))
        if file_path != path or mode != "rb":
            return handle
        wrapped = MagicMock(wraps=handle)
        wrapped.__enter__.return_value = wrapped

        def read(size: int) -> bytes:
            nonlocal bytes_read
            data = handle.read(size)
            bytes_read += len(data)
            return data

        wrapped.read.side_effect = read

        def close(*_args: object) -> None:
            handle.close()

        wrapped.__exit__.side_effect = close
        return wrapped

    with LinesConfig(path, sync_mode="none") as lines:
        with patch.object(Path, "open", tracked_open):
            assert list(lines.tail(1)) == [{"id": 2}]
        assert bytes_read <= 8192
        assert bytes_read < path.stat().st_size


def test_tail_handles_utf8_across_chunk_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    # The read boundary falls within a multibyte character of the selected line.
    value = "é" + "x" * 8189
    path.write_text(
        '{"id": 1}\n' + json.dumps({"text": value}, ensure_ascii=False), encoding="utf-8"
    )
    with LinesConfig(path, sync_mode="none") as lines:
        assert list(lines.tail(1)) == [{"text": value}]


def test_tail_ignores_partial_utf8_before_suffix(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    # The first byte of the last chunk is a UTF-8 continuation byte.
    prefix = b'\n{"id": 1}\n{"id": 2, "pad": "'
    ending = b'"}\n'
    padding = b"x" * (8191 - len(prefix) - len(ending))
    suffix = prefix + padding + ending
    path.write_bytes(b'{"text":"' + "é".encode() + suffix)
    with LinesConfig(path, sync_mode="none") as lines:
        assert list(lines.tail(2)) == [{"id": 1}, {"id": 2, "pad": padding.decode()}]


def test_tail_propagates_selected_data_errors(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    with LinesConfig(path, sync_mode="none") as lines:
        path.write_bytes(b'{"id": 1}\ninvalid\n')
        with pytest.raises(json.JSONDecodeError):
            list(lines.tail(1))
        path.write_bytes(b"\xff\n")
        with pytest.raises(UnicodeDecodeError):
            list(lines.tail(1))
        path.unlink()
        assert list(lines.tail(1)) == []

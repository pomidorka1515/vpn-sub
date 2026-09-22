from __future__ import annotations

from contextlib import AbstractContextManager
import sqlite3


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return dict(row) if row is not None else None


class ConnectionMixin:
    """Typed connection/transaction surface implemented by ``Database``."""

    def connection(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    def transaction(self, *, immediate: bool = False) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

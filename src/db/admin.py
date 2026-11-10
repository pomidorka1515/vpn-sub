from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from admin_permissions import validate_grants
from constants import SESSION_MAX_AGE

from .common import ConnectionMixin

if TYPE_CHECKING:
    import sqlite3

    from .common import Connection

type AdminKind = Literal["accounts", "tokens"]

ADMIN_SCHEMA = (
    """CREATE TABLE admin_identities (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('accounts', 'tokens')),
        name TEXT NOT NULL, secret TEXT NOT NULL, permissions TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(kind, name)
    )""",
    "CREATE UNIQUE INDEX idx_admin_token_secret ON admin_identities(secret) WHERE kind = 'tokens'",
    """CREATE TABLE admin_sessions (
        identity_id TEXT PRIMARY KEY REFERENCES admin_identities(id) ON DELETE CASCADE,
        cookie_hash TEXT NOT NULL UNIQUE, bearer_hash TEXT NOT NULL UNIQUE,
        expires_at INTEGER NOT NULL
    )""",
)


@dataclass(frozen=True)
class AdminIdentity:
    id: str
    kind: AdminKind
    name: str
    secret: str
    permissions: tuple[str, ...]
    revision: int


def _identity(row: sqlite3.Row) -> AdminIdentity:
    # Persisted data is not an alternative way to introduce unknown grants.
    kind = row["kind"]
    if kind not in {"accounts", "tokens"}:
        raise ValueError("Invalid admin identity kind")
    return AdminIdentity(
        str(row["id"]),
        kind,
        str(row["name"]),
        str(row["secret"]),
        tuple(validate_grants(json.loads(str(row["permissions"])))),
        int(row["revision"]),
    )


class AdminMixin(ConnectionMixin):
    async def admin_identities(self, kind: AdminKind) -> list[AdminIdentity]:
        async with self.connection() as conn:
            return [
                _identity(row)
                for row in await conn.rows(
                    "SELECT * FROM admin_identities WHERE kind = ? ORDER BY name, id", (kind,)
                )
            ]

    async def admin_identity(self, identity_id: str) -> AdminIdentity | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT * FROM admin_identities WHERE id = ?", (identity_id,))
            return _identity(row) if row else None

    async def admin_login_identity(self, name: str) -> AdminIdentity | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT * FROM admin_identities WHERE kind = 'accounts' AND name = ?", (name,)
            )
            return _identity(row) if row else None

    async def admin_bearer_identity(self, digest: str) -> AdminIdentity | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT * FROM admin_identities WHERE kind = 'tokens' AND secret = ? "
                "UNION ALL SELECT i.* FROM admin_sessions s JOIN admin_identities i ON s.identity_id = i.id "
                "WHERE i.kind = 'accounts' AND s.bearer_hash = ? AND s.expires_at > ? LIMIT 1",
                (digest, digest, int(time.time())),
            )
            return _identity(row) if row else None

    async def admin_cookie_identity(self, digest: str) -> AdminIdentity | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT i.* FROM admin_identities i JOIN admin_sessions s ON s.identity_id = i.id "
                "WHERE i.kind = 'accounts' AND s.cookie_hash = ? AND s.expires_at > ?",
                (digest, int(time.time())),
            )
            return _identity(row) if row else None

    async def admin_start_session(
        self, identity: AdminIdentity, cookie_hash: str, bearer_hash: str
    ) -> bool:
        if identity.kind != "accounts":
            return False
        async with self.transaction(immediate=True) as conn:
            if not await self._admin_revision_matches(conn, identity):
                return False
            await conn.execute(
                "INSERT INTO admin_sessions(identity_id, cookie_hash, bearer_hash, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(identity_id) DO UPDATE SET cookie_hash = excluded.cookie_hash, "
                "bearer_hash = excluded.bearer_hash, expires_at = excluded.expires_at",
                (identity.id, cookie_hash, bearer_hash, int(time.time()) + SESSION_MAX_AGE),
            )
            return True

    async def admin_end_session(self, cookie_hash: str) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute("DELETE FROM admin_sessions WHERE cookie_hash = ?", (cookie_hash,))

    @staticmethod
    async def _admin_revision_matches(conn: Connection, identity: AdminIdentity | None) -> bool:
        if identity is None:
            return True  # Config-backed root; never stored in this table.
        row = await conn.row("SELECT revision FROM admin_identities WHERE id = ?", (identity.id,))
        return row is not None and int(row[0]) == identity.revision

    async def admin_save_identity(
        self,
        identity: AdminIdentity,
        *,
        actor: AdminIdentity | None,
        create: bool = False,
    ) -> bool:
        async with self.transaction(immediate=True) as conn:
            if not await self._admin_revision_matches(conn, actor):
                return False
            if create:
                await conn.execute(
                    "INSERT INTO admin_identities(id, kind, name, secret, permissions) VALUES (?, ?, ?, ?, ?)",
                    (
                        identity.id,
                        identity.kind,
                        identity.name,
                        identity.secret,
                        json.dumps(identity.permissions),
                    ),
                )
                return True
            changed = await conn.execute(
                "UPDATE admin_identities SET name = ?, secret = ?, permissions = ?, revision = revision + 1 "
                "WHERE id = ? AND kind = ? AND revision = ?",
                (
                    identity.name,
                    identity.secret,
                    json.dumps(identity.permissions),
                    identity.id,
                    identity.kind,
                    identity.revision,
                ),
            )
            if changed:
                await conn.execute(
                    "DELETE FROM admin_sessions WHERE identity_id = ?", (identity.id,)
                )
            return bool(changed)

    async def admin_delete_identity(
        self, identity: AdminIdentity, *, actor: AdminIdentity | None
    ) -> bool:
        async with self.transaction(immediate=True) as conn:
            if not await self._admin_revision_matches(conn, actor):
                return False
            return bool(
                await conn.execute(
                    "DELETE FROM admin_identities WHERE id = ? AND revision = ?",
                    (identity.id, identity.revision),
                )
            )

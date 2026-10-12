from __future__ import annotations

from errors import MigrationError

from .async_common import AsyncConnectionMixin
from .schema import SCHEMA_STATEMENTS, SchemaMixin


class AsyncSchemaMixin(AsyncConnectionMixin):
    SCHEMA_VERSION = SchemaMixin.SCHEMA_VERSION

    async def initialize(self) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)")
            await conn.execute("INSERT INTO schema_version(id, version) VALUES (1, 0) ON CONFLICT(id) DO NOTHING")
            row = await conn.row("SELECT version FROM schema_version WHERE id = 1")
            if row is None:
                raise MigrationError("schema_version row is missing")
            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise MigrationError("schema version is not an integer") from exc
            if version < 0:
                raise MigrationError(f"invalid database schema version {version}")
            if version > self.SCHEMA_VERSION:
                raise MigrationError(f"database schema {version} is newer than supported {self.SCHEMA_VERSION}")
            if version == 0:
                tables = {str(row[0]) for row in await conn.rows("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_version'")}
                if tables:
                    raise MigrationError(f"unversioned database contains existing tables: {', '.join(sorted(tables))}")
                for statement in SCHEMA_STATEMENTS:
                    await conn.execute(statement)
                version = self.SCHEMA_VERSION
            if version == 1:
                await conn.execute("UPDATE bandwidth_snapshots SET up = max(up, 0), down = max(down, 0), wl_up = max(wl_up, 0), wl_down = max(wl_down, 0)")
                version = 2
            if version == 2:
                await conn.execute("ALTER TABLE users ADD COLUMN auth_token TEXT")
                await conn.execute("CREATE UNIQUE INDEX idx_users_auth_token ON users(auth_token)")
                version = 3
            if version == 3:
                await conn.execute("INSERT INTO app_metadata(key, value) VALUES ('admin_ui_session', '') ON CONFLICT(key) DO NOTHING")
            await conn.execute("UPDATE schema_version SET version = ? WHERE id = 1", (self.SCHEMA_VERSION,))

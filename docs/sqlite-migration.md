# SQLite state migration

## Ownership and inventory

`config.json` remains the authority for deployment and presentation settings:
panel connections, domains and routes, API/bot credentials, profile templates,
nodes, fingerprints allowed by the service, text, and other feature settings.

SQLite is the only runtime authority after migration:

| Legacy JSON state | Readers and writers | SQLite destination |
| --- | --- | --- |
| `users`, `tokens`, `displaynames`, `userFingerprints` | `Subscription`; API and bots through `Subscription` | `users.username`, `uuid`, unique `token`, `displayname`, `fingerprint` |
| `status`, `statusTime`, `statusWl`, `time` | `Subscription.update_user`, subscription rendering, `BWatch` | `users.enabled`, `enabled_time`, `enabled_wl`, `expires_at` |
| `bw`, `wl_bw` | user info, leaderboard, subscription headers, `BWatch` | quota/usage columns on `users` |
| `webui_users`, `webui_passwords` | web API and public bot login/settings | unique `users.ext_username`, `ext_password_hash` |
| `tgids` | public bot login/logout and notification lookup | one-to-one `telegram_mappings` |
| `publicbot.tg_lang` | public bot language selection | `telegram_preferences` |
| `codes` including `uses` and `perma` | admin API/bot, registration and bonus flows | `codes`; consumption uses `BEGIN IMMEDIATE` transactions |
| `_notified`, `_wl_notified` | `BWatch` warning deduplication | `notification_state` unique by kind and Telegram ID |
| `_last_reset`, `_last_reset_month` | `BWatch.is_first` | `app_metadata`; reset and marker update are one transaction |
| `bw_history.json` user snapshots | charts, API, bots, `BWatch` | `bandwidth_snapshots`, unique `(username, ts)`, indexed by timestamp |
| `snaps.json` system/panel snapshots | admin state API, `BWatch` | `state_snapshots`, unique `ts`; payload preserves the typed nested metrics |

`audit.jsonl` deliberately remains JSONL. It is an append-only operational
audit stream exposed by an existing tail/read API, not mutable project state.
`log.jsonl` likewise remains an append-only diagnostic log. No runtime code
reads migrated state from legacy JSON after startup migration.

Foreign keys cascade user deletion into Telegram mappings, daily bandwidth
snapshots, and pending registration records. Unique constraints cover UUIDs,
subscription tokens, external usernames, Telegram IDs and code names. Queries
used by APIs and charts specify deterministic ordering.

## Connection and transaction model

`Database` opens a connection per operation/transaction instead of sharing a
connection between Flask and background threads. Every connection enables
foreign keys, WAL, normal synchronization and a busy timeout. Writes use short
transactions; panel and other network calls occur outside them.

Registration code consumption plus local user creation, bonus application plus
code consumption, monthly resets, warning deduplication, user deletion and
snapshot upserts are atomic. Registration creates a short-lived pending sync
row. Successful panel synchronization confirms it; a failed synchronization
deletes the user and refunds a finite code use in one transaction.

The `schema_version` table records version 1. `Database.initialize()` is
idempotent. Future schema changes must increment `SCHEMA_VERSION` and add an
ordered migration before application code starts using new columns or tables.

## One-time import

Startup calls `migrate_legacy` before strict `Config` loading. The database path
is selected from `SUB_DB_PATH`, then optional static `database_path`, then
`../state.sqlite3`.

The importer takes an exclusive migration lock and:

1. Reads and validates the legacy objects.
2. Copies every present source into `../backup/migration` (or the explicitly
   supplied backup directory) with a UTC timestamp and
   `.migration-backup` suffix.
3. Initializes SQLite and imports all dynamic rows in one write transaction.
4. Validates user, code and Telegram row counts and records
   `legacy_migration=complete` in that same transaction.
5. Atomically removes the migrated keys from `config.json`, including nested
   `publicbot.tg_lang`. Historical bandwidth/snapshot files are retained.

The completion marker makes re-runs no-ops. If import fails, its transaction is
rolled back, config is not scrubbed, and startup fails rather than running with
mixed authority. If config rewriting fails after the committed import, the next
startup sees the marker and retries only the idempotent config cleanup.
Telegram mappings and bandwidth histories that refer to already-deleted users
are skipped, counted, and reported as orphaned records. Their source files and
backups remain available for inspection; valid records are imported normally.
If legacy state contains several external login aliases for one user, the first
alias is retained and the rest are counted as skipped records because the new
schema intentionally permits one login per user. Duplicate snapshot timestamps
use the last value from the source file, matching the old runtime's upsert
behavior. Signed bandwidth deltas are preserved because panel counter resets can
legitimately produce them.

## Operator procedure

1. Stop the service so no older binary can write JSON during migration.
2. Ensure the service account can write the database directory, `config.json`,
   and `../backup/migration`.
3. Optionally set `database_path` in config or `SUB_DB_PATH` in the service
   environment. Start the updated service normally; migration is automatic.
4. Verify startup reports imported counts, the database exists, migrated keys
   are absent from config, and `/api/user/list`, user login, code listing and
   history endpoints return expected data.
5. Keep the timestamped migration backups and legacy `bw_history.json` and
   `snaps.json` until operational verification is complete. They are never
   automatically deleted.

For recovery from a failed import, correct the reported source-data problem and
restart. The unmarked transaction contains no imported rows. For an operator
rollback after a successful import, stop the service, deploy the previous
version, restore `config.json` from its migration backup, and restore the legacy
history files if they were changed manually. Preserve the SQLite database for
forensics; remove it only after verifying the rollback.

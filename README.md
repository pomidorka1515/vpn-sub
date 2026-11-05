# vpn-sub

Self-hosted VPN subscription manager for [3x-ui](https://github.com/MHSanaei/3x-ui) panels.  
Serves VLESS subscription links and JSON profiles, tracks per-user traffic and expiry, hands out access through invite and bonus codes, and provides admin and user interfaces on the web, Telegram, and Discord.  
Built with Python 3.14 and Quart. HTTP, core services, panel I/O, and SQLite all share one asyncio event loop, together with the background bandwidth watcher and the optional bots.  
SQLite is the source of truth for users, codes, quotas, and bandwidth. `config.json` is deployment and presentation only.  
Designed to run on one small Linux VPS. See [docs/ASYNC_PACKAGING.md](docs/ASYNC_PACKAGING.md) for packaging details.

## Features

- **Multi-panel user lifecycle.** Create, update, reset, and delete users on every
  configured panel from one place. Panel writes are rolled back if the database commit fails. A
  failed registration rollback is marked and retried at the next startup. UUID
  updates are not rolled back the same way: the first failing panel is recorded,
  and later panels may already hold the new UUID. The UUID failure marker blocks
  further user mutations, including deletion and reconciliation. A pending reset
  is replayed from its durable old/new UUID intent at startup or by reset/reconcile;
  only successful panel writes and a SQLite UUID commit clear its UUID marker.
  Standalone UUID failures require manual repair: restore every panel client to
  the user's SQLite UUID, verify agreement, then explicitly clear the `uuid`
  rollback marker through the admin rollback controls. Clearing a marker alone
  does not repair panel state; do not clear it before verification.
- **Invite and bonus codes.** Register codes and bonus codes grant days, monthly
  GB, and whitelist GB. One-shot or reusable. A bonus adds GB only to finite
  limits; 0 stays unlimited independently for main and whitelist allowances.
  Expiry 0 stays unlimited. A lapsed finite expiry
  restarts from now. Re-enable waits for the bandwidth watcher.
- **Whitelist panel.** Optional dedicated panel with its own quota, separate
  from the main traffic limit. Inbound lists can be whitelist or blacklist.
- **Subscription endpoint.** `GET /{uri}?token=&lang=` returns VLESS links, or a
  JSON profile for Happ and `force_json=1`. Language is `ru` or `en`. Per-user
  uTLS fingerprint. Browsers get an HTML page instead of a profile. QR is a
  separate WebAPI route.
- **Public web UI.** `res/` dashboard, history, and charts. WebAPI: register,
  login, bonus, stats, history, settings, logout, account delete. Cookie
  `auth_token`.
- **Admin UI and API.** Browser admin at `/{uri}/admin` (username/password
  session). Token API at `/{uri}/{api_uri}/api/...`: users, codes, panel health,
  audit log, snapshots, leaderboard, system status, and stuck-rollback markers.
  Header `Authorization`.
- **Telegram.** Both bots are optional. Omit `bot` or `publicbot`, or leave its
  token empty, and that bot is not started. Admin bot is UID-whitelisted: users,
  codes, panels, traffic, leaderboard. Public bot: link account, traffic, charts,
  subscription, bonus, settings, reset, delete. Quota notices and admin log
  alerts are skipped when the matching bot is off.
- **Discord.** Optional `discord` config section, one token. Public slash commands plus `/admin`
  for a Discord-ID whitelist.
  See [src/discord/README.md](src/discord/README.md).
- **Bandwidth watcher (`BWatch`).** Polls traffic about every 15s, checks quota
  and expiry about every 2 minutes, panel health about every 5 minutes. Daily
  bandwidth and state snapshots, with retry and admin alerts. Monthly reset
  check and inbound reconcile about every 2 hours. Panel polls fan out on a
  bounded async workers, with separate foreground/background capacity.  
  Quota counters are tracked per panel and account UUID. Two consecutive
  readings below the previous baseline, with the second at least as large as
  the first, confirm a reset: the full second counter is charged once. A lone
  low reading is ignored; missing readings interrupt reset confirmation.
  Baselines and pending resets persist atomically with quota usage. Legacy
  aggregate baselines are reinitialized on the first poll after upgrade (stored
  usage is retained, but traffic since the last legacy poll cannot be recovered
   reliably). Daily snapshots retain their separate aggregate-counter policy.
   Scheduled snapshots align to UTC midnight; startup and retry readings retain
   their actual collection timestamps. Bandwidth history shows deltas between
   successful readings, not calendar-day totals (downtime cannot be split across
   days from cumulative counters). Missing readings are not zero-traffic days.
- **Auth, audit, backups.** Argon2id passwords (legacy salted hashes still
  verify). JSONL audit trail. `config.json` validated against
  `config.schema.json` on load and commit; remote schemas are rejected.
  Scheduled backups of config and the SQLite database.
- **Shared rate limits.** Redis sliding window. Every request uses the
  same counters.

## Requirements

- Linux, glibc at least as new as Ubuntu 22.04 (`>= 2.35`)
- Local Redis on the same box; the app pings it at startup and exits if it is down
- 3x-ui v3 panels with admin-scoped API tokens
- Python 3.14 only for development; release binaries need no Python on the host

## Setup

Prefer the release binaries. No venv, no checkout, no `requirements.txt`.  
`data/` next to the binary holds runtime state. Fill `data/config.json` before
the first start. Add the optional `discord` section to enable Discord.  
A compiled binary ignores `$schema` and validates against the packed schema, so
the example's relative `$schema` is fine here.  
A git checkout is for development. See [Development](#development).

```bash
getconf GNU_LIBC_VERSION # ensure its >= 2.35
mkdir -p /opt/vpn-sub && cd /opt/vpn-sub
curl -fsSL -o vpn-sub https://github.com/pomidorka1515/vpn-sub/releases/latest/download/vpn-sub && chmod +x vpn-sub
mkdir -p data
curl -fsSL -o data/config.json https://raw.githubusercontent.com/pomidorka1515/vpn-sub/main/docs/EXAMPLE.config.json
apt install redis-server && systemctl enable --now redis-server
# fill in panel credentials, bot tokens, etc, then start the systemd unit below
```

### Redis

Rate limits are stored in Redis, not in process memory. One local instance is
enough; do not expose it.

```bash
apt install redis-server
systemctl enable --now redis-server
```

`config.json` must contain the URL the app connects to. `6379` is Redis's
default port, not something this app chooses:

```json
"redis": {
    "url": "redis://127.0.0.1:6379/0"
}
```

Start Redis before the HTTP service. If Redis dies later, rate-limited routes
return 429.

### 3x-ui panel auth

For initial setup, use `"3xui": {}` to run without panels. Configuration and  
DB-only operations remain available; panel-dependent operations (including user  
creation, deletion, resets, and live traffic) return `503 No panels configured`.  
Panel-dependent background jobs and recovery pause until panels are added and  
the service is restarted. Pending recovery records are preserved.  
At most one panel may have `"whitelist": true`; zero is also allowed. This  
selects the dedicated whitelist-traffic panel and is separate from each panel's  
inbound-list `mode`. Configurations with multiple whitelist panels are rejected.

Each panel in `3xui.*` authenticates with an **admin-scoped API token**
(Bearer) instead of a username/password login:

1. In the panel UI open **Settings → Security → API Token** and create a token
   with the `admin` scope (the `monitor` scope is read-only and gets rejected
   on every user mutation).
2. Put it in the panel's `token` config field (`username`/`password` are gone).
   Tokens are long random strings; the schema enforces `minLength: 20`.
3. A revoked or wrong token marks the panel dead with a `token rejected` reason
   in the logs and the admin-bot health alert — reissue the token in the panel
   and update the config; there is no auto-recovery.

This service targets the 3x-ui **v3 clients-first API** (`/panel/api/clients/*`,
one client per user with `email == username`). Upgrading the panel from 2.x?
After the panel upgrade, run the one-time reconciliation (service stopped):

```bash
venv/bin/python scripts/reconcile_clients.py          # dry-run report
venv/bin/python scripts/reconcile_clients.py --apply  # merge legacy clients
```

### Systemd service

```ini
[Unit]
Description=subscription backend
After=network.target redis-server.service
Wants=network.target redis-server.service

[Service]
User=root
WorkingDirectory=/opt/vpn-sub

# --- optional path overrides ---
# Environment="DIR_DATA=/var/lib/vpn-sub"
# Environment="DIR_RUNTIME=/var/lib/vpn-sub/run"
# Environment="DIR_BACKUPS=/var/backups/vpn-sub"
# Environment="PATH_CONFIG=/etc/vpn-sub/config.json"
# Environment="PATH_DB=/var/lib/vpn-sub/state.sqlite3"
# Environment="PATH_AUDIT=/var/log/vpn-sub/audit.jsonl"
# Environment="PATH_LANG=/path/to/vpn-sub/lang.jsonc"
# Environment="APP_BIND=127.0.0.1:5550"
# Environment="LOGLEVEL=INFO"  # recommended for prod
# Environment="LOGLEVEL_HTTP=INFO"

ExecStart=/opt/vpn-sub/vpn-sub

TimeoutStopSec=90
KillMode=mixed
KillSignal=SIGTERM

LimitNOFILE=65535
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Nginx location block

```nginx
location /sub {
	proxy_pass http://127.0.0.1:5550;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;

    proxy_hide_header Server;
    proxy_hide_header X-Powered-By;

	proxy_buffering    off; # this app uses SSE
	proxy_cache        off;
	proxy_read_timeout 86400s;

    proxy_http_version 1.1;
    proxy_set_header Connection        ""; # needed for keepalive upstream
}
```

The async HTTP entrypoint runs under Hypercorn behind nginx (TLS). Direct binds
are blocked when `REQUIRE_PROXY=1`; local requests from `127.0.0.1` and `::1`
are allowed. Binding to `0.0.0.0` or `::` is not recommended. Leave TLS and
public exposure to the reverse proxy; override at your own risk.

## Configuration

**See [example config](docs/EXAMPLE.config.json)**

`config.json` is validated against `config.schema.json` on load and on every
commit. Remote `$schema` URLs are rejected. Put panel tokens, bot tokens,
`api_token`, and the Redis URL here; do not commit the filled file.  
`bot` and `publicbot` are optional. Omit either key, or leave its token empty,
to run without that bot.

### Environment variables

All path variables are **optional**. If omitted, runtime data defaults to the
`./data/` folder inside the project directory.

| Variable        | Default                  | Description                                                             |
| :---            | :---                     | :---                                                                    |
| `DIR_DATA`      | `./data/`                | Base directory for runtime data (DB, logs, config)                      |
| `DIR_RUNTIME`   | `<DIR_DATA>/run/`        | Local directory for the primary lock, config locks, and inbound stamps. |
| `DIR_BACKUPS`   | `<DIR_DATA>/backup/`     | Directory where scheduled config backups are stored                     |
| `PATH_CONFIG`   | `<DIR_DATA>/config.json` | Path to the main application configuration                              |
| `PATH_DB`       | `<DIR_DATA>/state.sqlite3` | Path to the SQLite database                                           |
| `PATH_AUDIT`    | `<DIR_DATA>/audit.jsonl` | Path to audit trail logs (JSONL)                                        |
| `PATH_LANG`     | `./lang.jsonc`           | Path to the static UI language strings                                  |
| `REQUIRE_PROXY` | `1`                      | Whether to block requests which bypass a reverse proxy (recommended)    |
| `APP_BIND`      | `127.0.0.1:5550`         | Shared Hypercorn listener. |
| `LOGLEVEL`      | `DEBUG`                  | Threshold for application `Logger()` instances. |
| `VERBOSE_LOGGING_OVERRIDES` | *(unset)*   | Comma-separated components to start with verbose traces.                |

`LOGLEVEL` accepts a level name or an integer. Names
are case-insensitive: `TRACE`, `DEBUG`, `INFO`, `WARN` / `WARNING`, `ERROR`,
`CRITICAL` / `FATAL`. An integer is rounded down to the nearest level (`15`
is `DEBUG`, `0` is `TRACE`, `99` is `CRITICAL`). Unset, blank, or unrecognized
values use the default. ASGI access records use the `http.access` Python logger;  
`LOGLEVEL_HTTP` configures the redacted access logger (default `INFO`). Hypercorn's default access log is disabled in shared settings.

`VERBOSE_LOGGING_OVERRIDES` turns on the existing `verbose` flag for named
components: `bwatch`, `core` (`Subscription`), and `panels` (`XUiSession`,
including the whitelist panel). Names are case-insensitive and split on
commas; empty tokens are ignored, so `bwatch,`, `bwatch`, and `,` all parse.
Unknown names are ignored. Unset or empty leaves every component quiet.

### Path resolution for shipped files

Shipped files (`res/`, `lang.jsonc`) are found by walking up from the module
until one of those markers exists. In a checkout that is the repository root. In
a Nuitka payload it is the unpack directory, next to the compiled entry, not
next to the binary. `DIR_DATA` and the `PATH_*` variables still override runtime
files. When `DIR_DATA` is unset, a checkout uses `./data` in the working
directory. A frozen binary uses `data/` next to the executable (`sys.argv[0]`),
because the working directory is not the install directory and `__file__` is
inside the payload.

A compiled binary ignores `$schema` and validates `config.json` against the
packed `config.schema.json`. A checkout still resolves `$schema` relative to the
config file. From `data/config.json` the example `../config.schema.json` works
if `data/` sits next to the checkout. If you move `data/`, copy the schema next
to the config or fix `$schema`.

## Upgrades and migration

### Upgrading config to v6

v6 folds flags, links, nodes, and the other per-inbound maps into one object  
per profile. There is no compat path: the first start fails schema validation
until `config.json` has been migrated. `vpn-sub --update` replaces the binary
and does not touch the file. Stop the service first — a running v5 process  
reloads on file change and will misread the new objects.

```bash
python scripts/migrate_profiles.py            # dry-run
python scripts/migrate_profiles.py --apply    # rewrite config.json
```

The original is copied to `config.json.pre-v6` beside the file. A second  
`--apply` leaves that backup alone.

### SQLite schema 6 on upgrade

SQLite schema 6 separates administrative disables from quota state. Polling
restores only quota-disabled access. On upgrade, existing disabled sides stay
disabled until explicitly enabled, since their original disable reason is
unknown.

### Scripts

To load scripts for migration (3x-ui, config, etc), run `./vpn-sub --load-scripts`.
Pure migration scripts use stdlib; `reconcile_clients.py` requires the checkout's project venv and dependencies.

## Binaries

The release binaries are Nuitka onefile builds. The host that runs them needs
glibc at least as new as Ubuntu 22.04 (`>= 2.35`). Do not build them with a
free-threading interpreter (`python3.14t`, `sys.abiflags == "t"`). The workflow
refuses that ABI.

A tag `v*` builds one `vpn-sub` binary including Discord on `ubuntu-22.04` with CPython 3.14
(GIL build, empty `sys.abiflags`) and attaches it to the GitHub release.

`vpn-sub --probe` imports the HTTP and Discord runtime stack and checks the packed  
`lang.jsonc`, `discord.lang.jsonc`, `config.schema.json` and `res/` can be
opened. They do not boot the service. A missing config, a down Redis, or a
Discord login failure is not a probe failure.  
Release CI also verifies the Nuitka module report and runs the main binary from an unrelated working directory against disposable loopback Redis/panel dependencies, renders bundled templates, checks SQLite integrity, sends SIGTERM and restarts against the same state. No production credentials are required. See [packaging checks](docs/ASYNC_PACKAGING.md).

`vpn-sub --help` prints usage and exits. It does not  
boot the service. `-h` is the same flag. A checkout accepts it too.

`vpn-sub --update` replaces the installed binary
from the newest published version on GitHub, including prereleases, of `pomidorka1515/vpn-sub`. They only run from a
compiled binary, and only in a terminal. The command asks before it downloads.
If either service is running, it asks you to stop both and continues only after
they are gone. It does not stop or start them. A checkout (`python -m main`)
refuses. At boot a compiled binary logs an update notice when a newer release exists. It
does not download anything and it does not prompt.

> # !! WARNING !! PRERELEASES ARE NOT STABLE  
> `--update` includes GitHub RC, beta, alpha and other prereleases. The updater  
> displays a prominent warning before confirmation; boot notices warn too.

Release versions retain their full suffix (for example `6.0.0-rc.2+build.1`)  
in help output, script tag references and unpack directories. RC numbers compare  
numerically, and a stable version sorts after its prereleases. Build metadata  
does not affect update ordering. Tags with any `-suffix` are published as  
GitHub prereleases. Release notes use the tag annotation as-is.

`vpn-sub --load-scripts` writes the packed
`config.schema.json` beside the binary and downloads `scripts/` from the tag
this binary was built from. They only run from a compiled binary. A checkout
already has both and refuses. An existing file is replaced. The schema is the
one packed into that binary. The scripts are fetched and checked
against the tag's blob sha before they are written.

The unpack directory is `{CACHE_DIR}/<name>/{VERSION}/{PID}_{TIME_US}_{RANDOM}`
and is removed only after the child has exited. It is not shared across
restarts. A shared cache would be truncated by the next start while the running
process still had those `.so` files mapped.
`--onefile-child-grace-time=infinity` keeps the bootstrap from deleting that
directory during the 5s default grace window; systemd's `TimeoutStopSec` is what
bounds a stuck stop.

Discord starts and stops with the main systemd unit.

## Limitations

- Single VPS, Linux only. `Subscription` refuses to import on anything else.
- One Hypercorn serving process and one event loop. A file lock  
  (`<DIR_RUNTIME>/.primary.lock`) rejects secondary instances. Single-node design.  
- Local Redis is required. If it dies after startup, rate-limited routes fail
  closed with 429.
- Not highly available. Panel, database, and bots all live on the same box. A
  dead panel stays dead until you reissue the token and update config.
- 3x-ui v3 clients-first API only (`/panel/api/clients/*`, `email == username`).
  2.x needs the one-time reconcile above. No other panel software.
- Personal project. Untagged `main` is rolling development.
- Direct binds are blocked when `REQUIRE_PROXY=1`. TLS and public exposure
  belong on the reverse proxy. Application rate limits live in Redis.
- Python 3.14 for development and release builds; release binaries require the normal GIL ABI.

## Architecture

ASGI lifespan calls `await create_application()`: primary lock, validated config snapshots,  
SQLite, Redis, panel clients, `Subscription` recovery, watcher baseline/snapshots and optional Telegram bots. Startup failures unwind resources; readiness is published only after composition succeeds. Shutdown quiesces producers, settles mutations and closes resources, releasing the primary lock last.

- `src/app.py`: `create_application()` factory. Builds the `Application`
  runtime (paths, config snapshots, async DB, Redis, panels, subscription and Quart app)
- `src/asgi.py`: ASGI entrypoint (`asgi:app`); lifespan hooks start and stop the runtime
- `src/main.py`: process entry for a checkout (`python -m main`) and for the
  Nuitka binary. Runs Hypercorn in-process on one event loop
- `src/serve.py`: shared Hypercorn settings for checkout and binary serving
- `src/core/`: `Subscription` and the user, code, panel, bandwidth, and audit
  services. All user mutations go through `Subscription` to keep panels and
  SQLite consistent; the admin API, WebAPI, and Telegram bots already do, and
  Discord dispatches the same API handlers in-process
- `src/bwatch/`: `BWatch`: owned quota, expiry, panel health and snapshot tasks
- `src/session/`: `XUiSession`, buffered aiohttp transport and generation-safe caches
- `src/config/`: atomic JSON config with thread + cross-process locking
- `src/db/`: aiosqlite repository with write transaction ownership and leased reads. Users, codes, quotas, and bandwidth live here.  
  `config.json` is deployment and presentation only
- `src/api/`: Quart routes (`AsyncApi` for admin, `AsyncWebApi` for end users)
- `src/api/decorators/rate_limit.py`: Redis sliding-window limiter, shared by
  async HTTP caller
- `src/bots/telegram/`: Telegram `TelegramAdminBot` (management), `TelegramPublicBot` (user
  self-service)
- `src/bots/discord/`: optional Discord client owned by `create_application()`. One token serves public commands and
  `/admin`. See [src/discord/README.md](src/discord/README.md).
- `res/`: admin and user HTML. `docs/`: [public API](docs/API.md),
  [admin API](docs/API_ADMIN.md), [audit](docs/AUDIT.md)

## Development

A git checkout is for development. Start Redis first, then:

```bash
# Python 3.14 for development
python3.14 -m venv venv
venv/bin/pip install -r requirements.txt && venv/bin/pip install -e ".[dev]"
venv/bin/ruff check .
venv/bin/pytest
venv/bin/mypy && venv/bin/pyright
PYTHONPATH=src venv/bin/python -m main  # same entrypoint as the main binary
# Alternative checkout server (keep one worker):
PYTHONPATH=src venv/bin/hypercorn --config python:serve asgi:app
```

Ruff targets Python 3.14 and checks code style, modernization, common bugs, and  
banned legacy typing imports. Run `venv/bin/ruff check . --fix` to apply safe  
automatic fixes, then review the diff. CI runs `ruff check .` alongside pytest,  
mypy, and pyright, plus `venv/bin/python scripts/smoke_async_release.py --checkout`
with `redis-server` installed.

Tests in `src/tests/` are Python packages grouped by subsystem (`api/`, `core/`,
`bwatch/`, `config/`, `db/`, bots, and runtime). Run a subsystem or a single test:

```bash
venv/bin/pytest src/tests/core
venv/bin/pytest src/tests/core/test_credentials.py::test_valid_legacy_hash_is_migrated_to_argon2
venv/bin/pytest -m redis
venv/bin/pytest -m "not integration"
```

Keep reusable factories and fakes in `tests.support`, with explicit imports such
as `from tests.support.panels import FakePanel`. Put fixtures in the nearest
subsystem's `conftest.py`; the root only owns shared DB/config/language fixtures.
The database fixture owns a per-test subscription scope, so factory-created
subscriptions are closed before SQLite, including on failure. Tests that create
their own databases must close their subscriptions explicitly or use
`subscription_scope()`. Async fixtures and tests retain function-scoped loops.
Mark tests using external services or real network transports as `integration`;
tests requiring the local Redis executable also use `redis` and skip if absent.

HTTP contracts: [docs/API.md](docs/API.md) (cookie `auth_token`) and
[docs/API_ADMIN.md](docs/API_ADMIN.md) (`Authorization` header).

### Code quality

The codebase is kept deliberately small and boring to read:

- One asyncio event loop, one serving process, one SQLite writer with explicit
  transaction ownership
- PEP 695 generics, built-in generics, `collections.abc` / `contextlib` ABCs;
  legacy `TypeVar` / `Generic` / `typing_extensions` imports are banned by ruff
- Postponed annotations everywhere; mypy and pyright run clean in CI with the
  same strict ruff set
- Constant-time comparisons for tokens and secrets, atomic config writes with
  cross-process locking, JSONL audit trail for admin actions
- pytest suite covers core and DB behavior with function-scoped event loops
  and disposable SQLite files; a release smoke script boots the binary end to
  end against loopback dependencies

## Status

Personal project. Deployable by anyone who is familiar with linux and python.

## License

GPL v3

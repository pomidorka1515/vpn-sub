# vpn-sub

Self-hosted VPN subscription manager for [3x-ui](https://github.com/MHSanaei/3x-ui) panels.  
Serves VLESS subscription links and JSON profiles, tracks per-user traffic and expiry, hands out access through invite and bonus codes, and provides admin and user interfaces on the web, Telegram, and Discord.  
Built with Python 3.14 and Quart. HTTP, core services, panel I/O, and SQLite all share one asyncio event loop, together with the background bandwidth watcher and the optional bots.  
SQLite is the source of truth for users, codes, quotas, and bandwidth. `config.json` is deployment and presentation only.  
Designed to run on one small Linux VPS.

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
- **Public web UI.** `frontend/` dashboard, history, and charts. WebAPI: register,
  login, bonus, stats, history, settings, logout, account delete. Cookie
  `auth_token`. The user dashboard self-updates through a SSE endpoint.
- **Admin UI and API.** Browser admin at `/{uri}/admin` (username/password
  session). Token API at `/{uri}/{api_uri}/api/...`: users, codes, panel health,
  audit log, snapshots, leaderboard, system status, and stuck-rollback markers.
  Header `Authorization`.
  The first tab manages independent, editable admin accounts and API keys with
  scoped permissions. Existing config credentials remain protected `*` root
  identities without a config migration. See [permissions](docs/PERMISSIONS.md).
- **Telegram.** Both bots are optional. Omit `bot` or `publicbot`, or leave its
  token empty, and that bot is not started. Admin bot is UID-whitelisted: users,
  codes, panels, traffic, leaderboard. Public bot: link account, traffic, charts,
  subscription, bonus, settings, reset, delete. Quota notices and admin log
  alerts are skipped when the matching bot is off.
- **Discord.** Optional `discord` config section, one token. Public slash commands plus `/admin`
  for a Discord-ID whitelist.
  See [Discord setup](src/bots/discord/README.md).
  **Telegram and Discord admin operations are explicitly `*`-only** through their
  existing administrator whitelists; granular permissions apply only to HTTP.
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
  verify). JSONL audit trail: mutation audits wait for durability. Subscription
  hits are not logged to the audit trail.
  `LinesConfig(max_size_mb=100)` purges the entire JSONL file when an append
  exceeds 100 MB (decimal), including the triggering batch; no rotated copy is
  created. The limit is configurable per instance; existing backups are retained.
  Subscription traffic displays share panel samples for up to 15 seconds;
  watcher/dashboard samples replenish them, and panel-list parsing runs off-loop.
  Quota accounting continues to use fresh reads. `config.json` validated against
  `config.schema.json` on load and commit; remote schemas are rejected.
  Scheduled backups of config and the SQLite database.
- **Shared rate limits.** Process-local fixed-window counters keep the hot path
  free of Redis round trips; batched Redis reconciliation preserves shared state.
  Subscription downloads allow 60 requests/minute per token and 600/minute per
  client IP aggregate.

## Requirements

- 64-bit Linux (amd64 or aarch64), with a compatible glibc or musl; see [Binaries](#binaries)
- Local Redis on the same box; the app pings it at startup and exits if it is down
- 3x-ui v3 panels with admin-scoped API tokens
- Release binaries need no Python on the host; the v6 profile migration requires Python 3.14

## Setup

Prefer the release binaries. No venv, no checkout, no `requirements.txt`.  
`data/` next to the binary holds runtime state. Fill `data/config.json` before
the first start. Add the optional `discord` section to enable Discord.  
A compiled binary ignores `$schema` and validates against the packed schema, so
the example's relative `$schema` is fine here.  
A git checkout is for development. See [DEVELOPMENT.md](DEVELOPMENT.md).

Pick your preferred release asset for the machine's **architecture and libc**
from the [binary table](#binaries). The example below is for an amd64 glibc VPS;
change `binary` for another target. **Always save it as `vpn-sub`, without the
architecture/libc suffix** (for example, not `vpn-sub-amd64-glibc`). The service
and updater expect that installed name. No Python or Node installation is needed.

```bash
uname -m # x86_64 = amd64; aarch64 = aarch64
getconf GNU_LIBC_VERSION # glibc hosts; see the table for minimum versions
mkdir -p /opt/vpn-sub && cd /opt/vpn-sub
binary=vpn-sub-amd64-glibc # choose your preferred compatible asset
curl -fsSL -o vpn-sub "https://github.com/pomidorka1515/vpn-sub/releases/latest/download/$binary"
chmod +x vpn-sub
mkdir -p data
curl -fsSL -o data/config.json https://raw.githubusercontent.com/pomidorka1515/vpn-sub/main/docs/EXAMPLE.config.json
apt install redis-server && systemctl enable --now redis-server
# fill in panel credentials, bot tokens, etc, then start the systemd unit below
```

These service commands assume a Debian/Ubuntu VPS with systemd. For Alpine,
install Redis and zlib with `apk add redis zlib` and use your service manager
(for example OpenRC). For Android, follow the proot instructions below instead.
The `latest` URL selects a stable release; for a prerelease, select its tag on
the releases page and replace `latest/download` with `download/<tag>`.

### Redis

Rate limits are enforced in process memory first and periodically reconciled to
Redis. One local instance is enough; do not expose it.

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

Start Redis before the HTTP service. If Redis dies later, ordinary rate-limited
routes continue with their local fallback and do not return a store-generated
429. When a fixed-window limit is exceeded, HTTP 429 includes `Retry-After` in
seconds until the next window (rounded up). Admin authentication lockouts also
return 429 with `Retry-After` in seconds, while authentication store failures
return 503.

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
one client per user with `email == username`). There is no automatic update
path from 3x-ui 2.x: you need a clients-first panel already.

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

### Android / aarch64 proot (no systemd)

Use **`vpn-sub-aarch64-glibc` inside an Ubuntu 24.04 or newer proot guest**.
It is not a native Android/Termux executable: Android's bionic libc cannot load
it. Do not put it on `/sdcard` (shared storage does not support normal executable
permissions). Keep the install, data, and unpack cache inside the guest's private
filesystem. Proot is not a VM or a security boundary, and does not grant real root.

In Termux on a 64-bit ARM device:

```bash
pkg install proot-distro tmux
proot-distro install ubuntu
termux-wake-lock
tmux new -s vpn-sub
proot-distro login ubuntu
```

Run the following **inside Ubuntu**. Check `getconf GNU_LIBC_VERSION` reports
**2.39 or newer**; upgrade/reinstall an older guest before proceeding.

```bash
apt update
apt install -y ca-certificates curl redis-server supervisor
getconf GNU_LIBC_VERSION
mkdir -p /opt/vpn-sub/data/logs
cd /opt/vpn-sub
curl -fsSL -o vpn-sub https://github.com/pomidorka1515/vpn-sub/releases/latest/download/vpn-sub-aarch64-glibc
chmod +x vpn-sub
curl -fsSL -o data/config.json https://raw.githubusercontent.com/pomidorka1515/vpn-sub/main/docs/EXAMPLE.config.json
./vpn-sub --probe
# Edit data/config.json: panel credentials, optional bots, Redis URL, etc.
```

There is **no systemd in proot**. Use Supervisor to own Redis and the app instead.
Save this as `/opt/vpn-sub/supervisord.conf`:

```ini
[supervisord]
nodaemon=true
logfile=/opt/vpn-sub/data/logs/supervisord.log
pidfile=/opt/vpn-sub/data/supervisord.pid
childlogdir=/opt/vpn-sub/data/logs

[unix_http_server]
file=/opt/vpn-sub/data/supervisor.sock
chmod=0700

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///opt/vpn-sub/data/supervisor.sock

[program:redis]
command=/usr/bin/redis-server --bind 127.0.0.1 --protected-mode yes --save "" --appendonly no
priority=10
autorestart=true
stdout_logfile=/opt/vpn-sub/data/logs/redis.log
redirect_stderr=true

[program:vpn-sub]
command=/opt/vpn-sub/vpn-sub
directory=/opt/vpn-sub
environment=LOGLEVEL="INFO"
priority=20
autorestart=unexpected
startretries=10
stopsignal=TERM
stopwaitsecs=90
killasgroup=true
stdout_logfile=/opt/vpn-sub/data/logs/vpn-sub.log
redirect_stderr=true
```

Start it with `supervisord -c /opt/vpn-sub/supervisord.conf`. Leave that foreground
process running in the Termux tmux session; detach with **Ctrl-b, then d**, and
return with `tmux attach -t vpn-sub`. Redis starts first; the app retries if Redis
is not ready yet. Supervisor does not change the app's default loopback bind.
Use the reverse proxy described below for TLS/public access; do not expose Redis.

From a second Termux session, log into the **same** guest to manage or update it:

```bash
proot-distro login ubuntu
cd /opt/vpn-sub
supervisorctl -c supervisord.conf status
supervisorctl -c supervisord.conf stop vpn-sub
./vpn-sub --update
supervisorctl -c supervisord.conf start vpn-sub
# Stop everything gracefully before ending the original proot/tmux session:
# supervisorctl -c supervisord.conf shutdown
```

Disable Android battery optimization for Termux. A wake lock/tmux session does
not prevent Android from killing the app, nor survive a reboot; restart the
guest and Supervisor afterward. Host CPU/load/network counters may be blocked
by Android and display zero/empty values; these mean **unavailable**, not idle.
App RSS falls back to readable process counters when detailed memory is blocked.
Proot shares the Android kernel and network, so port conflicts are host-wide.

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
| `DIR_BACKUPS`   | `<DIR_DATA>/backup/`     | Directory where scheduled config and database backups are stored        |
| `PATH_CONFIG`   | `<DIR_DATA>/config.json` | Path to the main application configuration                              |
| `PATH_DB`       | `<DIR_DATA>/state.sqlite3` | Path to the SQLite database                                           |
| `PATH_AUDIT`    | `<DIR_DATA>/audit.jsonl` | Path to audit trail logs (JSONL)                                        |
| `PATH_LANG`     | `./lang.jsonc`           | Path to the static UI language strings                                  |
| `REQUIRE_PROXY` | `1`                      | Whether to block requests which bypass a reverse proxy (recommended)    |
| `APP_BIND`      | `127.0.0.1:5550`         | Shared Hypercorn listener. |
| `LOGLEVEL`      | `DEBUG`                  | Threshold for application `Logger()` instances. |
| `VERBOSE_LOGGING_OVERRIDES` | *(unset)*   | Comma-separated components to start with verbose traces.                |

Database backups use a `db-<sha256>` directory under the backup root, keyed by
the resolved absolute database path. Equivalent paths share a directory; moving
the database creates a new one. Older stem-only directories (such as `state/`)
remain untouched and are not included in automatic discovery or retention.

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

Shipped files (`frontend/dist/`, `lang.jsonc`) are found by walking up from the module
until one of those markers exists. In a checkout that is the repository root. In
a Nuitka payload it is the unpack directory, next to the compiled entry, not
next to the binary. `DIR_DATA` and the `PATH_*` variables still override runtime
files. When `DIR_DATA` is unset, a checkout uses `./data` in the working
directory. A frozen binary uses `data/` next to the executable (`sys.argv[0]`),
because the working directory is not the install directory and `__file__` is
inside the payload.

Release binaries include the built frontend; Node is not needed at runtime.
For checkout builds, see [frontend development](DEVELOPMENT.md#frontend-development).

A compiled binary ignores `$schema` and validates `config.json` against the
packed `config.schema.json`. A checkout still resolves `$schema` relative to the
config file. From `data/config.json` the example `../config.schema.json` works
if `data/` sits next to the checkout. If you move `data/`, copy the schema next
to the config or fix `$schema`.

## Upgrades and migration

### Permission-scoped administrators (minor release)

Existing `api_admin_ui_auth` and `api_token` continue to work unchanged as the
non-deletable root account and root key (`*`). SQLite upgrades automatically to
schema 8, adding independent scoped accounts, keys and sessions without changing
subscription data. Back up SQLite before upgrading; older binaries cannot open
schema 8. No manual config migration is needed for this feature. See
[permissions and API contracts](docs/PERMISSIONS.md), including delegation rules
and the intentionally root-only Telegram/Discord boundary.

### Upgrading config to v6

v6 folds flags, links, nodes, and the other per-inbound maps into one object  
per profile. There is no compat path: the first start fails schema validation
until `config.json` has been migrated. `vpn-sub --update` replaces the binary
and does not touch the file. Stop the service first — a running v5 process  
reloads on file change and will misread the new objects.

```bash
# From the binary installation directory; downloads scripts and the packed schema.
./vpn-sub --load-scripts
# Install Python 3.14 with venv and pip using your distribution's packages first.
python3.14 -m venv .migration-venv
.migration-venv/bin/python -m pip install 'fastjsonschema>=2.21,<3'
.migration-venv/bin/python scripts/migrate_profiles.py            # dry-run
.migration-venv/bin/python scripts/migrate_profiles.py --apply    # rewrite config.json
```

The original is preserved as `config.json.pre-v6` beside the file. A second
`--apply` leaves that backup alone. The replacement preserves the original file's
owner, group and mode, and fails if those cannot be preserved. Set `DIR_DATA`,
`PATH_CONFIG`, or pass `--config` if the config is not at `data/config.json`.

### SQLite schema 6 on upgrade

SQLite schema 6 separates administrative disables from quota state. Polling
restores only quota-disabled access. On upgrade, existing disabled sides stay
disabled until explicitly enabled, since their original disable reason is
unknown.

### Scripts

To load scripts for migration (3x-ui, config, etc), run `./vpn-sub --load-scripts`.
Migration scripts may require Python dependencies; see the instructions for each script.

## Binaries

All four release binaries include Discord, Python, and the built frontend:

| Release asset | Target / minimum host libc |
| :--- | :--- |
| `vpn-sub-amd64-glibc` | x86-64 Linux, glibc **2.35+** (Ubuntu 22.04+) — the existing build |
| `vpn-sub-amd64-musl` | x86-64 Linux, musl **1.2.5+** (Alpine 3.23 build baseline) |
| `vpn-sub-aarch64-glibc` | ARM64 Linux, glibc **2.39+** (Ubuntu 24.04+) — intended for **proot** |
| `vpn-sub-aarch64-musl` | ARM64 Linux, musl **1.2.5+** (Alpine 3.23 build baseline) |

Musl is **dynamically linked, not embedded/static**: install the matching musl
loader/libc and zlib (`apk add musl zlib` on Alpine). Old musl versions are not
supported. The glibc variants likewise use the guest/host's glibc and zlib.
Pick by the Linux **userspace** you run in, not merely the host kernel: Ubuntu
proot uses glibc, Alpine uses musl, and native Termux/bionic is unsupported.
`uname -m` identifies the architecture; `getconf GNU_LIBC_VERSION` identifies
glibc, while `ldd --version` on musl reports its version (it may exit nonzero).

Download your preferred compatible asset with **`curl -o vpn-sub`**, and keep the
installed filename **`vpn-sub` without the architecture/libc suffix**. For build and packaging details, see
[DEVELOPMENT.md](DEVELOPMENT.md#release-builds-and-packaging).

`vpn-sub --probe` imports the HTTP and Discord runtime stack and checks the packed  
`lang.jsonc`, `config.schema.json` and `frontend/dist/` can be
opened. It does not boot the service. A missing config, a down Redis, or a
Discord login failure is not a probe failure.  

`vpn-sub --help` prints usage and exits. It does not  
boot the service. `-h` is the same flag. A checkout accepts it too.

`vpn-sub --update` replaces the installed binary
from the newest published version on GitHub, including prereleases, of `pomidorka1515/vpn-sub`. It only runs from a
compiled binary, and only in a terminal. The command detects the running
architecture and libc, downloads the matching `vpn-sub-<arch>-<libc>` asset,
verifies its release SHA-256/size, and replaces **`vpn-sub`** (keeping
`vpn-sub.bak`). It refuses an unsupported ABI or missing matching asset rather
than guessing. The command asks before it downloads.
If the service is running, it asks you to stop it and continues only after
it is gone. It does not stop or start it. This also works without systemd,
including proot/Supervisor. A checkout (`python -m main`)
refuses. At boot a compiled binary logs an update notice when a newer release exists. It
does not download anything and it does not prompt. Set `VPN_SUB_DISABLE_UPDATE_NOTICE=1`
in the service environment to disable this startup network check; explicit
`--update` commands are unaffected.

Older releases whose updater only requests an asset named `vpn-sub` cannot
discover the new asset names. For the first upgrade from one of those releases,
stop the service and download the matching new asset manually with
`curl -o vpn-sub` as above, then `chmod +x vpn-sub`. Subsequent updates select the ABI
automatically. Back up the old binary and `data/` before that manual upgrade.

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

Discord starts and stops with the main process, whether managed by systemd or Supervisor.

## Limitations

- Single VPS, Linux only. `Subscription` refuses to import on anything else.
- One Hypercorn serving process and one event loop. A file lock  
  (`<DIR_RUNTIME>/.primary.lock`) rejects secondary instances. Single-node design.  
- Local Redis is required at startup. If it dies after startup, ordinary
  rate-limited routes use their local fallback; admin authentication storage
  failures still return 503.
- Not highly available. Panel, database, and bots all live on the same box. A
  dead panel stays dead until you reissue the token and update config.
- 3x-ui v3 clients-first API only (`/panel/api/clients/*`, `email == username`).
  Earlier panel versions are not supported. No other panel software.
- Personal project. Untagged `main` is rolling development.
- Direct binds are blocked when `REQUIRE_PROXY=1`. TLS and public exposure
  belong on the reverse proxy. Application rate limits use local counters with
  periodic Redis reconciliation.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for contributor setup, mandatory code
standards, architecture, frontend development, testing, and release builds.

## Status

Personal project. Deployable by anyone who is familiar with linux and python.

## License

GPL v3

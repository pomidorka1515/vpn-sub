# vpn-sub

VPN subscription management service. Flask + Telegram bots + a Discord bot +
3x-ui panel glue.  
Flask and Telegram are fully synchronous. Discord is a separate asyncio process.  
SQLite holds users, codes, and quotas. `config.json` is deployment and
presentation only.  
Designed to run on a single small VPS.

## What it does

- Manages users across multiple [3x-ui](https://github.com/MHSanaei/3x-ui)
  panels from one place
- Serves VLESS subscription links and JSON profiles, with per-user traffic and
  expiry in the client
- Tracks bandwidth and auto-disables users who exceed quota or expire, including
  a separate whitelist quota
- Telegram admin bot and public user bot, plus one Discord bot (public commands
  and `/admin`) that talks to Flask over loopback
- SQLite is the source of truth for users, codes, quotas, and bandwidth

## Key features

- **Multi-panel lifecycle.** Create, update, reset, and delete users on every
  configured panel. Panel writes are rolled back if the database commit fails. A
  failed registration rollback is marked and retried at the next startup. UUID
  updates are not rolled back the same way: the first failing panel is recorded,
  and later panels may already hold the new UUID.
- **Invite and bonus codes.** Register codes and bonus codes grant days, monthly
  GB, and whitelist GB. One-shot or reusable. A bonus adds GB onto a 0 limit (no
  allowance, not unlimited). Expiry 0 stays unlimited. A lapsed finite expiry
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
- **Discord.** Separate process, one token. Public slash commands plus `/admin`
  for a Discord-ID whitelist.
  See [src/discord/README.md](src/discord/README.md).
- **Bandwidth watcher (`BWatch`).** Polls traffic about every 15s, checks quota
  and expiry about every 2 minutes, panel health about every 5 minutes. Daily
  bandwidth and state snapshots, with retry and admin alerts. Monthly reset
  check and inbound reconcile about every 2 hours. Panel polls fan out on a
  background thread pool.
- **Auth, audit, backups.** Argon2id passwords (legacy salted hashes still
  verify). JSONL audit trail. `config.json` validated against
  `config.schema.json` on load and commit; remote schemas are rejected.
  Scheduled backups of config and the SQLite database.
- **Shared rate limits.** Redis sliding window. Every gunicorn thread uses the
  same counters.

## Limitations

- Single VPS, Linux only. `Subscription` refuses to import on anything else.
- One gunicorn worker. `src/gunicorn.conf.py` is `workers = 1`, `threads = 3`.
  Extra workers duplicate background threads and Telegram bots. A file lock
  (`<DIR_RUNTIME>/.primary.lock`) elects one primary; a process that loses the
  lock still serves HTTP but does not start `BWatch` or the bots. Not a
  multi-node design.
- Local Redis is required. The app pings it at startup and will not boot if it
  is down. A later outage fails rate-limited routes closed with 429.
- Not highly available. Panel, database, and bots all live on the same box. A
  dead panel stays dead until you reissue the token and update config.
- 3x-ui v3 clients-first API only (`/panel/api/clients/*`, `email == username`).
  2.x needs the one-time reconcile below. No other panel software.
- Discord does not share the process. It only talks to Flask over loopback.
  Start Flask first. Killing one does not stop the other.
- Personal project. Untagged `main` is rolling development. Tests are for this
  repo, not a supported public suite.
- Direct binds are blocked when `REQUIRE_PROXY=1`. TLS and public exposure
  belong on the reverse proxy. Application rate limits live in Redis.
- Python 3.12+ to start. Built for 3.13+.


## Architecture

Startup order is fixed and handled by `create_application()`: configs, database,
panels, `Subscription`, then `BWatch` and Telegram bots. Discord is not in that
list.

- `src/app.py` — `create_application()` factory: builds the `Application`
  runtime (paths, configs, DB, panels, subscription, watcher, Telegram bots,
  Flask app) and wires everything together
- `src/wsgi.py` — gunicorn entrypoint (`wsgi:app`); constructs the application
  and registers shutdown at exit
- `src/main.py` — process entry for a checkout (`python -m main`) and for the
  Nuitka binary. Runs gunicorn in-process with the same settings as
  `src/gunicorn.conf.py`
- `src/serve.py` — those gunicorn settings. The config file and `main` both read
  them
- `src/core/` — `Subscription` and the user, code, panel, bandwidth, and audit
  services
- `src/bwatch/` — `BWatch`: quota, expiry, panel health, snapshots
- `src/session/` — `XUiSession` panel HTTP client
- `src/config/` — atomic JSON config with thread + cross-process locking
- `src/db/` — SQLite. Users, codes, quotas, and bandwidth live here.
  `config.json` is deployment and presentation only
- `src/api/` — Flask routes (`Api` for admin, `WebApi` for end users)
- `src/api/decorators/rate_limit.py` — Redis sliding-window limiter, shared by
  every worker
- `src/bots/` — Telegram `AdminBot` (management), `PublicBot` (user
  self-service)
- `src/discord/` — Discord bots as a **separate process**. Not started by
  gunicorn or `create_application()`. One token serves public commands and
  `/admin`. See [src/discord/README.md](src/discord/README.md).
- `res/` — admin and user HTML. `docs/` — [public API](docs/API.md),
  [admin API](docs/API_ADMIN.md), [audit](docs/AUDIT.md)

## Setup

```bash
getconf GNU_LIBC_VERSION # ensure its >= 2.35
mkdir -p /opt/vpn-sub && cd /opt/vpn-sub
curl -fsSL -o vpn-sub https://github.com/pomidorka1515/vpn-sub/releases/latest/download/vpn-sub && chmod +x vpn-sub
curl -fsSL -o vpn-sub-discord https://github.com/pomidorka1515/vpn-sub/releases/latest/download/vpn-sub-discord && chmod +x vpn-sub-discord
mkdir -p data
curl -fsSL -o data/config.json https://raw.githubusercontent.com/pomidorka1515/vpn-sub/main/docs/EXAMPLE.config.json
curl -fsSL -o data/discord.json https://raw.githubusercontent.com/pomidorka1515/vpn-sub/main/src/discord/docs/EXAMPLE.config.json
apt install redis-server && systemctl enable --now redis-server
# fill in panel credentials, bot tokens, etc, then start the systemd units below
```

Prefer the release binaries. No venv, no checkout, no `requirements.txt`.  
The host needs glibc at least as new as Ubuntu 22.04 (`>=2.35`).  
`data/` next to the binaries is runtime state. Fill `data/config.json` before
the first start. `data/discord.json` is only needed if you run the Discord
process.  
A compiled binary ignores `$schema` and validates against the packed schema, so
the example's relative `$schema` is fine here.  
A git checkout is for development. See [Development](#development).

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

Start Redis before gunicorn. Startup pings that URL and exits if it is down. If
Redis dies later, rate-limited routes return 429.

### Scripts
To load scripts for migration (3x-ui, config, etc), run `./vpn-sub --load-scripts`.
Works from either binary. The scripts themselves do not depend on anything,
only stdlib — use system python to run them.

### 3x-ui panel auth

Each panel in `3xui.*` authenticates with an **admin-scoped API token**
(Bearer), not a username/password login:

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
python scripts/reconcile_clients.py          # dry-run report
python scripts/reconcile_clients.py --apply  # merge legacy clients
```

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

### Environment Variables

All path variables are **optional**. If omitted, runtime data defaults to the
`./data/` folder inside the project directory.

| Variable        | Default                  | Description                                                             |
| :---            | :---                     | :---                                                                    |
| `DIR_DATA`      | `./data/`                | Base directory for runtime data (DB, logs, config)                      |
| `DIR_RUNTIME`   | `<DIR_DATA>/run/`        | Local directory for the primary lock, config locks, and inbound stamps. |
| `DIR_BACKUPS`   | `<DIR_DATA>/backup/`     | Directory where scheduled config backups are stored                     |
| `PATH_CONFIG`   | `<DIR_DATA>/config.json` | Path to the main application configuration                              |
| `PATH_DB`       | `<DIR_DATA>/state.sqlite3` | Path to the SQLite database                                           |
| `PATH_LOG`      | `<DIR_DATA>/log.jsonl`   | Path to application event logs (JSONL)                                  |
| `PATH_AUDIT`    | `<DIR_DATA>/audit.jsonl` | Path to audit trail logs (JSONL)                                        |
| `PATH_LANG`     | `./lang.jsonc`           | Path to the static UI language strings                                  |
| `REQUIRE_PROXY` | `1`                      | Whether to block requests which bypass a reverse proxy (recommended)    |
| `GUNICORN_BIND` | `127.0.0.1:5550`         | Address gunicorn listens on (`src/gunicorn.conf.py`). Loopback only.    |

Shipped files (`res/`, `lang.jsonc`) are found by walking up from the module
until one of those markers exists. In a checkout that is the repository root. In
a Nuitka payload it is the unpack directory, next to the compiled entry, not
next to the binary. `DIR_DATA` and the `PATH_*` variables still override runtime
files. When `DIR_DATA` is unset, a checkout uses `./data` in the working
directory. A frozen binary uses `data/` next to the executable (`sys.argv[0]`),
because the working directory is not the install directory and `__file__` is
inside the payload.

### Systemd service

```ini
[Unit]
Description=subscription backend
After=network.target redis-server.service
Wants=network.target redis-server.service

[Service]
User=root
WorkingDirectory=X
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=src

# --- optional path overrides ---
# Environment="DIR_DATA=/var/lib/vpn-sub"
# Environment="DIR_RUNTIME=/var/lib/vpn-sub/run"
# Environment="DIR_BACKUPS=/var/backups/vpn-sub"
# Environment="PATH_CONFIG=/etc/vpn-sub/config.json"
# Environment="PATH_DB=/var/lib/vpn-sub/state.sqlite3"
# Environment="PATH_LOG=/var/log/vpn-sub/log.jsonl"
# Environment="PATH_AUDIT=/var/log/vpn-sub/audit.jsonl"
# Environment="PATH_LANG=/path/to/vpn-sub/lang.jsonc"
# Environment="GUNICORN_BIND=127.0.0.1:5550"

ExecStart=X/venv/bin/gunicorn \
    --config src/gunicorn.conf.py

TimeoutStopSec=35
KillMode=mixed
KillSignal=SIGTERM

LimitNOFILE=65535
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```
*(replace `X` with your path)*

`src/gunicorn.conf.py` loads `wsgi:app` (module `src/wsgi.py`, which calls
`create_application()` and registers shutdown at exit). The unit's working
directory must be the checkout. `PYTHONPATH=src` is what lets gunicorn import
`wsgi` and `serve`. `src/app.py` no longer runs anything at import time. A
quick local run is `PYTHONPATH=src venv/bin/python -m main`.

Do not pass `-w` or `--workers`. The config file already forces one worker. A
second process that wins the primary lock would start another `BWatch` and
another pair of Telegram bots.
Rate-limit counters are still shared if you do, because they live in Redis.

### Nginx location block
```
location /sub {
	proxy_pass http://127.0.0.1:5550;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;

    proxy_hide_header Server;
    proxy_hide_header X-Powered-By;

    proxy_http_version 1.1;
    proxy_set_header Connection        ""; # needed for keepalive upstream
}
```


### Example Config
**See [example config](docs/EXAMPLE.config.json)**

`config.json` is validated against `config.schema.json` on load and on every
commit. Remote `$schema` URLs are rejected. Put panel tokens, bot tokens,
`api_token`, and the Redis URL here; do not commit the filled file.
`bot` and `publicbot` are optional. Omit either key, or leave its token empty,
to run without that bot.

### Seemingly useless casts to protocols
All protocols in `src/custom_types.py` are fully compatible with their runtime
classes.
However, mypy cannot reliably validate that: the overloads are too complex.
That's why casting is required.

### Direct exposure
Binding this to `0.0.0.0` or `::` is not recommended.
The app automatically blocks direct hits (see `REQUIRE_PROXY` env variable),
but allows local requests made from `127.0.0.1` and `::1`.
Override this at your own risk: it's always best to leave TLS, etc. to reverse
proxies.

## Deployment
- Meant to run under gunicorn behind nginx (in front of the service, TLS).
  Application rate limits use local Redis.
- Systemd unit recommended for persistence
- Startup order matters: Redis → configs → DB → panels → Subscription → BWatch +
  Telegram bots (the app steps are handled by `create_application()`)
- Route every user mutation through `Subscription`. The admin API, WebAPI, and
  Telegram bots already do. Discord mutates users only by calling those HTTP
  APIs.
- A tag `v*` builds two onefile binaries on `ubuntu-22.04` with CPython 3.14
  (GIL build, empty `sys.abiflags`) and attaches them to the GitHub release.
  `vpn-sub` is this service. `vpn-sub-discord` is the Discord process.
  See [Binaries](#binaries).

### Binaries

The release binaries are Nuitka onefile builds. The host that runs them needs
glibc at least as new as Ubuntu 22.04 (`>=2.35`). Do not build them with a
free-threading interpreter (`python3.14t`, `sys.abiflags == "t"`). The workflow
refuses that ABI.

`vpn-sub --probe` and `vpn-sub-discord --probe` only check that the packed
`lang.jsonc` and `config.schema.json` (and, for the main binary, `res/`) can be
opened. They do not boot the service. A missing config, a down Redis, or a
Discord login failure is not a probe failure.

`vpn-sub --help` and `vpn-sub-discord --help` print usage and exit. They do not  
boot the service. `-h` is the same flag. A checkout accepts it too.

`vpn-sub --update` and `vpn-sub-discord --update` replace the installed binaries
from the latest GitHub release of `pomidorka1515/vpn-sub`. They only run from a
compiled binary, and only in a terminal. The command asks before it downloads.
If either service is running, it asks you to stop both and continues only after
they are gone. It does not stop or start them. A checkout (`python -m main`)
refuses. At boot a compiled binary logs one line when a newer release exists. It
does not download anything and it does not prompt.

`vpn-sub --load-scripts` and `vpn-sub-discord --load-scripts` write the packed
`config.schema.json` beside the binary and download `scripts/` from the tag
this binary was built from. They only run from a compiled binary. A checkout
already has both and refuses. An existing file is replaced. The schema is the
one packed into that binary, so the main binary and the Discord binary each
write their own. The scripts are not packed; they are fetched and checked
against the tag's blob sha before they are written.

A compiled binary ignores `$schema` and validates `config.json` against the
packed `config.schema.json`. A checkout still resolves `$schema` relative to the
config file. From `data/config.json` the example `../config.schema.json` works
if `data/` sits next to the checkout. If you move `data/`, copy the schema next
to the config or fix `$schema`.

```ini
[Service]
WorkingDirectory=X
Environment=DIR_DATA=X/data
Environment=GUNICORN_BIND=127.0.0.1:5550
ExecStart=X/vpn-sub
```

No `PYTHONPATH` and no `venv/bin/gunicorn`. Do not add `--workers`. The frozen
entry already sets `workers = 1` and `threads = 3`. Stop with SIGTERM. Do not
send SIGUSR2: gunicorn's graceful re-exec launches `sys.executable`, which
inside the payload is the unpacked child, not the onefile stub, so the new
process has no bootstrap and no packed files. SIGHUP reloads gunicorn config
in-process and is fine.

The unpack directory is `{CACHE_DIR}/<name>/{VERSION}/{PID}_{TIME_US}_{RANDOM}`
and is removed only after the child has exited. It is not shared across
restarts. A shared cache would be truncated by the next start while the running
process still had those `.so` files mapped.
`--onefile-child-grace-time=infinity` keeps the bootstrap from deleting that
directory during the 5s default grace window; systemd's `TimeoutStopSec` is what
bounds a stuck stop.

The Discord unit is the same shape, with `ExecStart=X/vpn-sub-discord` and
`SUB_HTTP_URL`, `SUB_URI`, and `SUB_API_URI` set to the Flask side. Start Flask
first.

## Development

```bash
venv/bin/pytest
venv/bin/mypy && venv/bin/pyright
venv/bin/python -m src.wsgi   # local run; production is the systemd unit above
```

HTTP contracts: [docs/API.md](docs/API.md) (cookie `auth_token`) and
[docs/API_ADMIN.md](docs/API_ADMIN.md) (`Authorization` header).

## Status
Personal project. Works in production for my small user base.

## License
GPL v3

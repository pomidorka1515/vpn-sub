# vpn-sub

VPN subscription management service. Flask + Telegram bots + a Discord bot + 3x-ui panel glue.  
Flask and Telegram are fully synchronous. Discord is a separate asyncio process.  
SQLite holds users, codes, and quotas. `config.json` is deployment and presentation only.  
Designed to run on a single small VPS.

## What it does

- Manages users across multiple [3x-ui](https://github.com/MHSanaei/3x-ui) panels from one place
- Serves VLESS subscription links and JSON profiles, with per-user traffic and expiry in the client
- Tracks bandwidth and auto-disables users who exceed quota or expire, including a separate whitelist quota
- Telegram admin bot and public user bot, plus one Discord bot (public commands and `/admin`) that talks to Flask over loopback
- SQLite is the source of truth for users, codes, quotas, and bandwidth

## Key features

- **Multi-panel lifecycle.** Create, update, reset, and delete users on every configured panel. Panel writes are rolled back if the database commit fails. A failed registration rollback is marked and retried at the next startup. UUID updates are not rolled back the same way: the first failing panel is recorded, and later panels may already hold the new UUID.
- **Invite and bonus codes.** Register codes and bonus codes grant days, monthly GB, and whitelist GB. One-shot or reusable. A bonus adds GB onto a 0 limit (no allowance, not unlimited). Expiry 0 stays unlimited. A lapsed finite expiry restarts from now. Re-enable waits for the bandwidth watcher.
- **Whitelist panel.** Optional dedicated panel with its own quota, separate from the main traffic limit. Inbound lists can be whitelist or blacklist.
- **Subscription endpoint.** `GET /{uri}?token=&lang=` returns VLESS links, or a JSON profile for Happ and `force_json=1`. Language is `ru` or `en`. Per-user uTLS fingerprint. Browsers get an HTML page instead of a profile. QR is a separate WebAPI route.
- **Public web UI.** `res/` dashboard, history, and charts. WebAPI: register, login, bonus, stats, history, settings, logout, account delete. Cookie `auth_token`.
- **Admin UI and API.** Browser admin at `/{uri}/admin` (username/password session). Token API at `/{uri}/{api_uri}/api/...`: users, codes, panel health, audit log, snapshots, leaderboard, system status, and stuck-rollback markers. Header `Authorization`.
- **Telegram.** Admin bot is UID-whitelisted: users, codes, panels, traffic, leaderboard. Public bot: link account, traffic, charts, subscription, bonus, settings, reset, delete.
- **Discord.** Separate process, one token. Public slash commands plus `/admin` for a Discord-ID whitelist. See [src/discord/README.md](src/discord/README.md).
- **Bandwidth watcher (`BWatch`).** Polls traffic about every 15s, checks quota and expiry about every 2 minutes, panel health about every 5 minutes. Daily bandwidth and state snapshots, with retry and admin alerts. Monthly reset check and inbound reconcile about every 2 hours. Panel polls fan out on a background thread pool.
- **Auth, audit, backups.** Argon2id passwords (legacy salted hashes still verify). JSONL audit trail. `config.json` validated against `config.schema.json` on load and commit; remote schemas are rejected. Scheduled backups of config and the SQLite database.
- **Shared rate limits.** Redis sliding window. Every gunicorn thread uses the same counters.

## Limitations

- Single VPS, Linux only. `Subscription` refuses to import on anything else.
- One gunicorn worker. `src/gunicorn.conf.py` is `workers = 1`, `threads = 3`. Extra workers duplicate background threads and Telegram bots. A file lock (`<DIR_RUNTIME>/.primary.lock`) elects one primary; a process that loses the lock still serves HTTP but does not start `BWatch` or the bots. Not a multi-node design.
- Local Redis is required. The app pings it at startup and will not boot if it is down. A later outage fails rate-limited routes closed with 429.
- Not highly available. Panel, database, and bots all live on the same box. A dead panel stays dead until you reissue the token and update config.
- 3x-ui v3 clients-first API only (`/panel/api/clients/*`, `email == username`). 2.x needs the one-time reconcile below. No other panel software.
- Discord does not share the process. It only talks to Flask over loopback. Start Flask first. Killing one does not stop the other.
- Personal project. Untagged `main` is rolling development. Tests are for this repo, not a supported public suite.
- Direct binds are blocked when `REQUIRE_PROXY=1`. TLS and public exposure belong on the reverse proxy. Application rate limits live in Redis.
- Python 3.12+ to start. Built for 3.13+. A free-threading build is optional and only logged at startup.

## Core principles

- **"No tag on commit = don't expect stability."**
  - Untagged commits on `main` are rolling development. If you want stability, only check out released tags.
- **"Works fine on my machine"**
  - Self-explanatory. This is primarily built and tested for my own setup.
  - Doesn't start? Something broke? Feel free to open an issue and I'll likely look into it when I can.
- **Tests are intended only for me.**
  - They contain hardcoded paths, etc. That's intentional.

## Architecture

Startup order is fixed and handled by `create_application()`: configs, database, panels, `Subscription`, then `BWatch` and Telegram bots. Discord is not in that list.

- `src/app.py` — `create_application()` factory: builds the `Application` runtime (paths, configs, DB, panels, subscription, watcher, Telegram bots, Flask app) and wires everything together
- `src/wsgi.py` — gunicorn entrypoint (`wsgi:app`); constructs the application and registers shutdown at exit
- `src/core/` — `Subscription` and the user, code, panel, bandwidth, and audit services
- `src/bwatch/` — `BWatch`: quota, expiry, panel health, snapshots
- `src/session/` — `XUiSession` panel HTTP client
- `src/config/` — atomic JSON config with thread + cross-process locking
- `src/db/` — SQLite. Users, codes, quotas, and bandwidth live here. `config.json` is deployment and presentation only
- `src/api/` — Flask routes (`Api` for admin, `WebApi` for end users)
- `src/api/decorators/rate_limit.py` — Redis sliding-window limiter, shared by every worker
- `src/bots/` — Telegram `AdminBot` (management), `PublicBot` (user self-service)
- `src/discord/` — Discord bots as a **separate process**. Not started by gunicorn or `create_application()`. One token serves public commands and `/admin`. See [src/discord/README.md](src/discord/README.md).
- `res/` — admin and user HTML. `docs/` — [public API](docs/API.md), [admin API](docs/API_ADMIN.md), [audit](docs/AUDIT.md)

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
apt install redis-server && systemctl enable --now redis-server
mkdir -p data && cp docs/EXAMPLE.config.json data/config.json  # fill in panel credentials, bot tokens, etc
# optional Discord bots (one token, public commands plus /admin):
cp src/discord/docs/EXAMPLE.config.json data/discord.json  # fill public.token, private.whitelist, private.api_token
# run systemd services; explained below
```

Always use `venv/bin/...`, never system Python. Runtime state lives in `data/` and is gitignored.

### Redis

Rate limits are stored in Redis, not in process memory. One local instance is enough; do not expose it.

```bash
apt install redis-server
systemctl enable --now redis-server
```

`config.json` must contain the URL the app connects to. `6379` is Redis's default port, not something this app chooses:

```json
"redis": {
    "url": "redis://127.0.0.1:6379/0"
}
```

Start Redis before gunicorn. Startup pings that URL and exits if it is down. If Redis dies later, rate-limited routes return 429.

### 3x-ui panel auth

Each panel in `3xui.*` authenticates with an **admin-scoped API token**
(Bearer), not a username/password login:

1. In the panel UI open **Settings → Security → API Token** and create a
   token with the `admin` scope (the `monitor` scope is read-only and gets
   rejected on every user mutation).
2. Put it in the panel's `token` config field (`username`/`password` are
   gone). Tokens are long random strings; the schema enforces `minLength: 20`.
3. A revoked or wrong token marks the panel dead with a `token rejected`
   reason in the logs and the admin-bot health alert — reissue the token in
   the panel and update the config; there is no auto-recovery.

This service targets the 3x-ui **v3 clients-first API** (`/panel/api/clients/*`,
one client per user with `email == username`). Upgrading the panel from 2.x?
After the panel upgrade, run the one-time reconciliation (service stopped):

```bash
venv/bin/python scripts/reconcile_clients.py          # dry-run report
venv/bin/python scripts/reconcile_clients.py --apply  # merge legacy clients
```

### Environment Variables

All path variables are **optional**. If omitted, runtime data defaults to the `./data/` folder inside the project directory.

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

`src/gunicorn.conf.py` loads `src.wsgi:app` (module `src/wsgi.py`, which calls
`create_application()` and registers shutdown at exit). `src/app.py` no longer runs
anything at import time, so `venv/bin/python -m src.wsgi` also works for a quick
local run.

Do not pass `-w` or `--workers`. The config file already forces one worker. A second process that wins the primary lock would start another `BWatch` and another pair of Telegram bots.
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

`config.json` is validated against `config.schema.json` on load and on every commit. Remote `$schema` URLs are rejected. Put panel tokens, bot tokens, `api_token`, and the Redis URL here; do not commit the filled file.

### Seemingly useless casts to protocols
All protocols in `src/custom_types.py` are fully compatible with their runtime classes.
However, mypy cannot reliably validate that: the overloads are too complex.
That's why casting is required.

### Direct exposure
Binding this to `0.0.0.0` or `::` is not recommended.
The app automatically blocks direct hits (see `REQUIRE_PROXY` env variable),
but allows local requests made from `127.0.0.1` and `::1`.
Override this at your own risk: it's always best to leave TLS, etc. to reverse proxies.

## Deployment
- Meant to run under gunicorn behind nginx (in front of the service, TLS). Application rate limits use local Redis.
- Systemd unit recommended for persistence
- Startup order matters: Redis → configs → DB → panels → Subscription → BWatch + Telegram bots (the app steps are handled by `create_application()`)
- Route every user mutation through `Subscription`. The admin API, WebAPI, and Telegram bots already do. Discord mutates users only by calling those HTTP APIs.

## Development

```bash
venv/bin/pytest
venv/bin/mypy && venv/bin/pyright
venv/bin/python -m src.wsgi   # local run; production is the systemd unit above
```

HTTP contracts: [docs/API.md](docs/API.md) (cookie `auth_token`) and [docs/API_ADMIN.md](docs/API_ADMIN.md) (`Authorization` header).

## Status
Personal project. Works in production for my small user base.

## License
GPL v3

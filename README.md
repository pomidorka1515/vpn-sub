# vpn-sub

VPN subscription management service. Flask + Telegram bots + Discord bots + 3x-ui panel glue.  
Flask and Telegram are fully synchronous. Discord is a separate asyncio process.  
Database-backed config, designed to run on a single small VPS.

## What it does

- Manages users across multiple [3x-ui](https://github.com/MHSanaei/3x-ui) panels from one place
- Serves VLESS subscription links with custom per-user traffic and expiry info
- Tracks bandwidth and auto-disables users who exceed quota or expire
- Two Telegram bots: admin panel and public user-facing bot
- Discord bots (public and admin) as a separate process; talks to Flask over loopback
- SQLite database as a source of truth

## Key features

- Multi-panel user lifecycle: create, update, reset, delete, with panel writes rolled back if the database commit fails
- Invite codes and bonus codes (days, monthly GB, whitelist GB; one-shot or reusable)
- Optional dedicated whitelist panel, separate from the main traffic quota
- Subscription endpoint at `/{uri}`: VLESS links, JSON profiles, QR, language and fingerprint selection
- Public web dashboard (`res/`) and WebAPI: register, login, stats, history, settings, account delete
- Admin API (token header): users, codes, panel health, audit log, snapshots, leaderboard
- Telegram admin bot (whitelist) and public bot (link account, traffic, subscription, settings)
- Discord as a second process, one token, public commands plus `/admin`. See [src/discord/README.md](src/discord/README.md)
- Bandwidth watcher (`BWatch`): quota enforcement, expiry, panel health alerts, daily bandwidth and state snapshots
- Argon2id passwords, JSONL audit trail, schema-validated config, scheduled config backups

## Limitations

- Single VPS, Linux only. `Subscription` refuses to import on anything else.
- One gunicorn process. `src/gunicorn.conf.py` is `workers = 1`, `threads = 3`. Extra workers duplicate background threads and Telegram bots. A file lock (`data/.primary.lock`) elects one primary; it is not a multi-node design.
- Not highly available. Panel, database, and bots all live on the same box. A dead panel stays dead until you reissue the token and update config.
- 3x-ui v3 clients-first API only (`/panel/api/clients/*`, `email == username`). 2.x needs the one-time reconcile below. No other panel software.
- Discord does not share the process. It only talks to Flask over loopback. Start Flask first. Killing one does not stop the other.
- Personal project. Untagged `main` is rolling development. Tests are for this repo, not a supported public suite.
- Direct binds are blocked when `REQUIRE_PROXY=1`. TLS, rate limits, and public exposure belong on the reverse proxy.

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
- `src/bwatch.py` — `BWatch`: quota, expiry, panel health, snapshots
- `src/session.py` — `XUiSession` panel HTTP client
- `src/config/` — atomic JSON config with thread + cross-process locking
- `src/db/` — SQLite. Users, codes, quotas, and bandwidth live here. `config.json` is deployment and presentation only
- `src/api/` — Flask routes (`Api` for admin, `WebApi` for end users)
- `src/bots/` — Telegram `AdminBot` (management), `PublicBot` (user self-service)
- `src/discord/` — Discord bots as a **separate process**. Not started by gunicorn or `create_application()`. One token serves public commands and `/admin`. See [src/discord/README.md](src/discord/README.md).
- `res/` — admin and user HTML. `docs/` — [public API](docs/API.md), [admin API](docs/API_ADMIN.md), [audit](docs/AUDIT.md)

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
mkdir -p data && cp docs/EXAMPLE.config.json data/config.json  # fill in panel credentials, bot tokens, etc
# optional Discord bots (one token, public commands plus /admin):
cp src/discord/docs/EXAMPLE.config.json data/discord.json  # fill public.token, private.whitelist, private.api_token
# run systemd services; explained below
```

Always use `venv/bin/...`, never system Python. Runtime state lives in `data/` and is gitignored.

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

| Variable        | Default                  | Description                                                         |
| :---            | :---                     | :---                                                                |
| `DIR_DATA`      | `./data/`                | Base directory for runtime data (DB, logs, config)                  |
| `DIR_BACKUPS`   | `<DIR_DATA>/backup/`     | Directory where scheduled config backups are stored                 |
| `PATH_CONFIG`   | `<DIR_DATA>/config.json` | Path to the main application configuration                          |
| `PATH_DB`       | `<DIR_DATA>/state.db`    | Path to the SQLite database                                         |
| `PATH_LOG`      | `<DIR_DATA>/log.jsonl`   | Path to application event logs (JSONL)                              |
| `PATH_AUDIT`    | `<DIR_DATA>/audit.jsonl` | Path to audit trail logs (JSONL)                                    |
| `PATH_LANG`     | `./lang.jsonc`           | Path to the static UI language strings                              |
| `REQUIRE_PROXY` | `1`                      | Whether to block requests which bypass a reverse proxy (recommended) |

### Systemd service

```ini
[Unit]
Description=subscription backend
After=network.target
Wants=network.target

[Service]
User=root
WorkingDirectory=X
Environment=PYTHONUNBUFFERED=1

# --- optional path overrides ---
# Environment="DIR_DATA=/var/lib/vpn-sub"
# Environment="DIR_BACKUPS=/var/backups/vpn-sub"
# Environment="PATH_CONFIG=/etc/vpn-sub/config.json"
# Environment="PATH_DB=/var/lib/vpn-sub/state.db"
# Environment="PATH_LOG=/var/log/vpn-sub/log.jsonl"
# Environment="PATH_AUDIT=/var/log/vpn-sub/audit.jsonl"
# Environment="PATH_LANG=/path/to/vpn-sub/lang.jsonc"

ExecStart=X/venv/bin/gunicorn \
    --bind 127.0.0.1:X \
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
*(replace `X` with your path/port/etc)*

`src/gunicorn.conf.py` loads `src.wsgi:app` (module `src/wsgi.py`, which calls
`create_application()` and registers shutdown at exit). `src/app.py` no longer runs
anything at import time, so `venv/bin/python -m src.wsgi` also works for a quick
local run.

Do not pass `-w` or `--workers`. The config file already forces one worker. A second process would start another `BWatch` and another pair of Telegram bots.

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

`config.json` is validated against `config.schema.json` on load and on every commit. Remote `$schema` URLs are rejected. Put panel tokens, bot tokens, and `api_token` here; do not commit the filled file.

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
- Meant to run under gunicorn behind nginx (in front of the service, rate-limiting and TLS)
- Systemd unit recommended for persistence
- Startup order matters: configs → DB → panels → Subscription → BWatch + Telegram bots (handled automatically by `create_application()`)
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

# vpn-sub

VPN subscription management service. Flask + Telegram bots + 3x-ui panel glue.
Fully synchronous, database-backed config, designed to run on a single small VPS.

## What it does

- Manages users across multiple [3x-ui](https://github.com/MHSanaei/3x-ui) panels
  from one place
- Serves VLESS subscription links with custom per-user traffic/expiry info
- Tracks bandwidth and auto-disables users who exceed quota or expire
- Two Telegram bots: admin panel and public user-facing bot
- SQLite database as a source of truth

## Core principles

- **"No tag on commit = don't expect stability."**
  - Untagged commits on `main` are rolling development. If you want stability, only check out released tags.
- **"Works fine on my machine"**
  - Self-explanatory. This is primarily built and tested for my own setup.
  - Doesn't start? Something broke? Feel free to open an issue and I'll likely look into it when I can.
- **Tests are intended only for me.**
  - They contain hardcoded paths, etc. That's intentional.

## Architecture

- `app.py` — `create_application()` factory: builds the `Application` runtime (paths, configs, DB, panels, subscription, watcher, bots, Flask app) and wires everything together
- `wsgi.py` — gunicorn entrypoint (`wsgi:app`); constructs the application and registers shutdown at exit
- `core.py` — `Subscription`, `BWatch`, `XUiSession` (the heart)
- `config.py` — atomic JSON config with thread + cross-process locking
- `db.py` — core database logic
- `api.py` — Flask routes (`Api` for admin, `WebApi` for end users)
- `bots.py` — `AdminBot` (management), `PublicBot` (user self-service)

## Setup

```bash
pip install -r requirements.txt
mkdir -p data && cp docs/EXAMPLE.config.json data/config.json  # fill in panel credentials, bot tokens, etc
# run a systemd service; explained below
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
    --config gunicorn.conf.py

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

`gunicorn.conf.py` loads `wsgi:app` (module `wsgi.py`, which calls
`create_application()` and registers shutdown at exit). `app.py` no longer runs
anything at import time, so `venv/bin/python wsgi.py` also works for a quick
local run.

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

### Seemingly useless casts to protocols
All protocols in `custom_types.py` are fully compatible with their runtime classes.
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
- Startup order matters: configs → DB → panels → Subscription → BWatch + bots (handled automatically by `create_application()`)

## Status
Personal project. Works in production for my small user base.

## License
GPL v3

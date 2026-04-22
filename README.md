# vpn-sub

VPN subscription management service. Flask + Telegram bots + 3x-ui panel glue.
Fully synchronous, file-backed config, designed to run on a single small VPS.

## What it does

- Manages users across multiple [3x-ui](https://github.com/MHSanaei/3x-ui) panels
  from one place
- Serves VLESS subscription links with custom per-user traffic/expiry info
- Tracks bandwidth and auto-disables users who exceed quota or expire
- Two Telegram bots: admin panel and public user-facing bot
- JSON config as the source of truth (no database)

## Architecture

- `core.py` — `Subscription`, `BWatch`, `XUiSession` (the heart)
- `config.py` — atomic JSON config with thread + cross-process locking
- `api.py` — Flask routes (`Api` for admin, `WebApi` for end users)
- `bots.py` — `AdminBot` (management), `PublicBot` (user self-service)
- `app.py` — wiring

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json  # fill in panel credentials, bot tokens, etc
gunicorn --threads 4 -b 127.0.0.1:5550 app:app
```

**Note:** Use `--threads` (not `-w`/`--workers`) — the app uses file locking to elect a primary worker for background tasks (BWatch bandwidth monitor, bots). Multiple *processes* will each try to start background threads, which is wasteful; multiple *threads* within one process works correctly.

Config is validated against `config.schema.json` (pointed to by `"$schema"` in the config file) on every load and commit. Remote schemas are rejected; the schema is cached and only re-read when the file changes.

## Deployment
- Meant to run under gunicorn behind nginx
- Systemd unit recommended for persistence
- Startup order matters: panels → Subscription → BWatch + bots (handled automatically by `app.py`)

## Status
Personal project. Works in production for my small user base. Not stable..

## License
GPL v3

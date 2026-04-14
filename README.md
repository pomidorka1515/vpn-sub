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

<details>
<summary>Click to expand diagram</summary>

​```
┌──────────┐   ┌──────────┐   ┌─────────┐
│ AdminBot │   │PublicBot │   │ WebApi  │
└────┬─────┘   └────┬─────┘   └────┬────┘
│              │              │
└──────────────┼──────────────┘
▼
┌───────────────┐
│  Subscription │ ← core logic
└───────┬───────┘
│
┌───────────┼───────────┐
▼           ▼           ▼
┌────────┐ ┌────────┐  ┌────────┐
│3x-ui #1│ │3x-ui #2│  │  ...   │
└────────┘ └────────┘  └────────┘
```

</details>
- `core.py` — `Subscription`, `BWatch`, `XUiSession` (the heart)
- `config.py` — atomic JSON config with thread + cross-process locking
- `api.py` — Flask routes (`Api` for admin, `WebApi` for end users)
- `bots.py` — `AdminBot` (management), `PublicBot` (user self-service)
- `app.py` — wiring

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json  # fill in panel credentials, bot tokens, etc
gunicorn -w 4 -b 127.0.0.1:5550 app:app
```
See config.schema.json for the full config shape.
## Deployment
- Meant to run under gunicorn behind nginx
- Systemd unit recommended for persistence

## Status
Personal project. Works in production for my small user base. Not stable..

## License
TODO

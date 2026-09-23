# Discord bot service

This is a **separate process**. It is not started by gunicorn or `create_application()`. Killing Flask does not kill this unit, and vice versa.

Independent Discord bots for the VPN subscription service.
Same venv, project root cwd, `PYTHONPATH=src/discord/src:src`. Talks to Flask over loopback HTTP.

Two bots: public (real), admin (stub).

## Core principles

*see main README*

## Setup

```bash
pip install -r requirements.txt
mkdir -p data && cp src/discord/docs/EXAMPLE.config.json data/discord.json
# fill public.token; leave private as {}
PYTHONPATH=src/discord/src:src venv/bin/python -m app
```

The example already has `$schema` set for `data/discord.json`.  

HTTP contract: see root [docs/API.md](../../docs/API.md).

## Environment Variables

All path variables are **optional**.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DIR_DATA` | `./data/` | Runtime directory |
| `PATH_LOG` | `<DIR_DATA>/log.jsonl` | Shared JSONL with Flask |
| `PATH_DISCORD_CONFIG` | `<DIR_DATA>/discord.json` | This service's config |
| `PATH_DISCORD_LANG` | `src/discord/lang.jsonc` | Strings (relative to project root) |
| `PATH_DISCORD_SESSIONS` | `<DIR_DATA>/discord-sessions.json` | Auth cookies per Discord user |
| `SUB_HTTP_URL` | `http://127.0.0.1:5550` | Flask bind (loopback) |
| `SUB_URI` | `sub` | Same as main `cfg["uri"]` |

WebAPI prefix: `{SUB_HTTP_URL}/{SUB_URI}/webapi`.

## Systemd service

```ini
[Unit]
Description=subscription discord bots
After=network.target
Wants=network.target

[Service]
User=root
WorkingDirectory=X
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=src/discord/src:src
Environment=SUB_HTTP_URL=http://127.0.0.1:X
ExecStart=X/venv/bin/python -m app
TimeoutStopSec=35
KillMode=mixed
KillSignal=SIGTERM
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

*(replace `X` with your values)*

The Flask unit should already be up so WebAPI exists at boot. 


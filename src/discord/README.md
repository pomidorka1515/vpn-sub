# Discord bot service

This is a **separate process**. It is not started by gunicorn or `create_application()`. Killing Flask does not kill this unit, and vice versa.

Independent Discord bots for the VPN subscription service.
Same venv, project root cwd, `PYTHONPATH=src/discord/src:src`. Talks to Flask over loopback HTTP.

One Discord application / one bot token. Public slash commands stay as they are; `/admin` is an extra command on the same identity.

## Core principles

*see main README*

## Setup

```bash
pip install -r requirements.txt
mkdir -p data && cp src/discord/docs/EXAMPLE.config.json data/discord.json
# fill public.token, private.whitelist, private.api_token
PYTHONPATH=src/discord/src:src venv/bin/python -m app
```

The example already has `$schema` set for `data/discord.json`.

- `public.token` — Discord bot token (one client).
- `private.whitelist` — Discord user IDs allowed to use `/admin`.
- `private.api_token` — same value as Flask `cfg["api_token"]`. Sent as the `Authorization` header to the admin API.

HTTP contract: see root [docs/API.md](../../docs/API.md) and [docs/API_ADMIN.md](../../docs/API_ADMIN.md).

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
| `SUB_API_URI` | `privapi` | Same as main `cfg["api_uri"]` |

WebAPI prefix: `{SUB_HTTP_URL}/{SUB_URI}/webapi`.  
Admin API prefix: `{SUB_HTTP_URL}/{SUB_URI}/{SUB_API_URI}`.  
The admin API class is `src/api/admin.py` `Api`; its mount is `/{uri}/{api_uri}`, not `/privapi` as a hardcoded name.  
Empty `SUB_API_URI` is omitted, which hits `/api/...` instead of `/{api_uri}/api/...`.

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
Environment=SUB_API_URI=privapi
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

The Flask unit should already be up so WebAPI / admin API exist at boot.

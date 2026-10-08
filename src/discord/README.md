# Discord bot service

This is a **separate process**. It is not started by gunicorn or
`create_application()`. Killing Flask does not kill this unit, and vice versa.

Independent Discord bots for the VPN subscription service.
Same venv, project root cwd, `PYTHONPATH=src/discord/src:src`. Talks to Flask
over loopback HTTP.

One Discord application / one bot token. Public slash commands stay as they are;
`/admin` is an extra command on the same identity.

## Core principles

*see main README*

## Setup

```bash
pip install -r requirements.txt
mkdir -p data && cp src/discord/docs/EXAMPLE.config.json data/discord.json
# fill public.token, private.whitelist, private.api_token
PYTHONPATH=src/discord/src:src venv/bin/python -m runtime
```

`runtime` is the process entry for a checkout and for a Nuitka binary. Do not
compile `app.py`: it rewrites `sys.path` from the checkout layout, which does
not exist inside the payload.

The example already has `$schema` set for `data/discord.json`.

- `public.token` — Discord bot token (one client).
- `private.whitelist` — Discord user IDs allowed to use `/admin`.
- `private.api_token` — same value as Flask `cfg["api_token"]`. Sent as the
  `Authorization` header to the admin API.

HTTP contract: see root [docs/API.md](../../docs/API.md)
and [docs/API_ADMIN.md](../../docs/API_ADMIN.md).

## Environment Variables

All path variables are **optional**.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DIR_DATA` | `./data/` | Runtime directory |
| `PATH_LOG` | `<DIR_DATA>/log.jsonl` | Shared JSONL with Flask |
| `PATH_DISCORD_CONFIG` | `<DIR_DATA>/discord.json` | This service's config |
| `PATH_DISCORD_LANG` | checkout: `<root>/src/discord/lang.jsonc`; binary: packed `lang.jsonc` | Strings. A checkout walks up to the repository root. A Nuitka payload includes that file next to the compiled entry. |
| `PATH_DISCORD_SESSIONS` | `<DIR_DATA>/discord-sessions.json` | Auth cookies per Discord user |
| `SUB_HTTP_URL` | `http://127.0.0.1:5550` | Flask bind (loopback) |
| `SUB_URI` | `sub` | Same as main `cfg["uri"]` |
| `SUB_API_URI` | `privapi` | Same as main `cfg["api_uri"]` |
| `LOGLEVEL` | `DEBUG` | Threshold for `Logger()`. Same variable as Flask. |

`LOGLEVEL` is case-insensitive (`TRACE`, `DEBUG`, `INFO`, `WARN` / `WARNING`,
`ERROR`, `CRITICAL` / `FATAL`) or an integer rounded down to the nearest
level. Unset, blank, or unrecognized values stay `DEBUG`. This process has no
gunicorn access log, so `LOGLEVEL_GUNICORN` does nothing here.

WebAPI prefix: `{SUB_HTTP_URL}/{SUB_URI}/webapi`.  
Admin API prefix: `{SUB_HTTP_URL}/{SUB_URI}/{SUB_API_URI}`.  
The admin API class is `src/api/admin` `Api`; its mount is
`/{uri}/{api_uri}`, not `/privapi` as a hardcoded name.  
Empty `SUB_API_URI` is omitted, which hits `/api/...` instead of
`/{api_uri}/api/...`.

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
Environment=SUB_URI=sub
Environment=SUB_API_URI=privapi
ExecStart=X/venv/bin/python -m runtime
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

A release binary is `vpn-sub-discord` from the same tag as `vpn-sub`. It does
not need `PYTHONPATH`. `DIR_DATA` defaults to `data/` next to the executable,
not to a directory inside the unpack tree. `vpn-sub-discord --probe` checks that
the packed language file and schema open. It does not log in and does not
require `discord.json`. A compiled binary ignores `$schema` and validates
against the packed schema. A checkout still resolves `$schema` relative to
`data/discord.json` (`../src/discord/config.schema.json`). If you move `data/`,
copy the schema next to the config or fix `$schema`.

`vpn-sub-discord --help` prints usage and exits. It does not log in. `-h` is  
the same flag. A checkout accepts it too.

`vpn-sub-discord --load-scripts` writes that packed schema beside the binary
and downloads `scripts/` from the tag it was built from. Same flag as
`vpn-sub --load-scripts`. A checkout refuses.

# Async packaging and deployment (step 11)  
  
## Dependencies and retirement  
  
`requirements.txt` and `pyproject.toml` declare the same runtime requirements. Quart and Hypercorn are the active direct framework/server dependencies; aiohttp, aiosqlite, redis.asyncio and pyTelegramBotAPI >=4.37,<5 provide runtime I/O. Requests remains for CLI downloads and the staged reconcile script. pytest-asyncio is development-only.  
  
The explicit Flask, Gunicorn and types-gunicorn requirements are annotated retirement items for the dedicated step 12 cleanup commit. Legacy core/server/logging tests still require them today. Removing the direct Flask requirement does not remove Quart's upstream Flask dependency. Legacy server files and synchronous repository/panel/core implementations retire together after remaining caller migration. `GUNICORN_BIND` and `LOGLEVEL_GUNICORN` compatibility aliases expire at step 12 cutover; generic variables win when both are present.  
  
## Release contract  
  
The main Nuitka onefile build explicitly includes ASGI, Quart, Hypercorn, aiohttp, aiosqlite, redis.asyncio and telebot async modules. It bundles `res/`, `lang.jsonc`, `config.schema.json` and certifi data. No WSGI/Gunicorn entrypoint is explicitly included. Nuitka 4.x is the supported build major; the existing Python 3.14 compression workaround fails visibly if its target changes.  
  
Discord excludes Quart/Hypercorn, Flask, telebot, aiosqlite and Redis and retains aiohttp for its HTTP client. Its own language/schema files are bundled. Both binaries run import/resource probes, and XML compilation reports are retained as separate CI artifacts. The main probe imports the entire async dependency stack without starting runtime I/O.  
  
`scripts/smoke_async_release.py --binary <path> --report <report.xml>` checks module inclusion and runs the actual compiled entrypoint from a temporary working directory. It creates an isolated configuration and database, starts private loopback Redis and an authenticated panel fake, disables optional bots by omitting their config, and exercises:  
  
- Watcher startup panel listing and initial state snapshots.  
- Admin session creation through the real Redis Lua limiter and SQLite repository.  
- Authenticated admin HTML rendering from bundled resources.  
- SIGTERM, zero exit status and database integrity.  
- Restart with the same state and primary lock, followed by another clean SIGTERM.  
  
The harness never reads production state or contacts production panel/Telegram endpoints. The normal compiled updater may perform its bounded public release-version lookup at boot. Temporary state is deleted after the processes close. The main release job requires this smoke check before uploading the binary; the Discord job requires its independent resource probe.  
  
## Checkout verification  
  
```bash
venv/bin/python scripts/smoke_async_release.py --checkout
PYTHONPATH=src venv/bin/python -m main --probe
```
  
The checkout smoke runs the same real composition/server/shutdown path. CI installs redis-server for this test. Deployment uses one process/event loop, `APP_BIND`, `LOGLEVEL_HTTP`, and the README systemd example with `TimeoutStopSec=90`, `KillMode=mixed` and SIGTERM. Do not enable reload or multiple workers. The shared checkout command is `PYTHONPATH=src venv/bin/hypercorn --config python:serve asgi:app`.  
  
## Verification status  
  
Local verification: 642 pytest tests, mypy and pyright pass; checkout startup/auth/template/SQLite/SIGTERM/restart smoke passes. The local interpreter is CPython 3.14.7 free-threaded (`sys.abiflags == "t"`), which the release workflow correctly rejects. A normal-ABI Nuitka build and the compiled smoke checks must pass in release CI before step 11 acceptance is complete. No release-grade compiled binary was verified locally.  

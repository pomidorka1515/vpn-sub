# Async server and entrypoints (step 10)  
  
`src/asgi.py` exports an inert Quart application and an injectable `create_asgi_application()` factory. Import registers lifespan/readiness hooks but does not load runtime configuration, open connections, acquire the primary lock, or start polling. Lifespan startup composes the runtime, publishes it in `app.extensions`, and admits HTTP work only when it is ready. Failed composition retains the existing reverse-order unwind and secondary instances refuse startup.  
  
## Serving and signals  
  
Both serving paths use `src/serve.py`:  
  
```bash
PYTHONPATH=src venv/bin/python -m main
PYTHONPATH=src venv/bin/hypercorn --config python:serve asgi:app
```
  
The binary uses `main.serve_application()` with one `asyncio.run()` and Hypercorn's in-process API. The checkout CLI config selects one asyncio worker; do not override its worker count or enable reload in deployment. Hypercorn alone owns SIGINT/SIGTERM, stops listener admission, and drains HTTP requests before invoking Quart shutdown. No competing signal handlers are installed.  
  
`APP_BIND` defaults to `127.0.0.1:5550`. `LOGLEVEL_HTTP` defaults to INFO; invalid log levels use INFO. The temporary Gunicorn environment aliases were retired during step 12 cleanup. Middleware owns redacted HTTP access logging; Hypercorn access logging is disabled in both paths.  
  
## Shutdown budgets and ordering  
  
- Startup lifespan: 120 seconds.  
- HTTP graceful drain: 10 seconds.  
- Runtime shutdown: 60 seconds; Hypercorn lifespan budget: 65 seconds.  
- systemd `TimeoutStopSec`: 90 seconds, leaving termination margin.  
  
Runtime stop closes task/mutation admission and marks readiness false. Explicit resource quiesce hooks stop bot polling/settle handlers and cancel watcher loops before the mutation registry drains. Panel/Telegram sessions, database, audit and file offloads remain available during settlement. Resources then close in reverse dependency order, with the primary lock last. Repeated stops share the same owned cleanup task. Timeout propagates as failed lifespan shutdown; it does not imply successful settlement or authorize closing connections under unfinished operations. systemd remains the ultimate hard deadline.  
  
## Verification and staging  
  
`src/tests/test_asgi_server.py` runs real Hypercorn subprocesses for checkout CLI and the binary's programmatic serving function against a controlled lifespan dependency. Both serve an HTTP request, receive SIGTERM, execute shutdown, and exit successfully. Settings precedence, readiness rejection, and lifespan ownership are covered; `test_app_runtime.py` verifies producers quiesce before mutations and connection closure.  
  
Verification: Ruff, mypy and pyright pass; full pytest suite passes with 642 tests. Actual Nuitka compilation/resource/start-stop acceptance belongs to step 11.  
  
Cleanup: Gunicorn worker/settings, logging, dependencies/stubs and temporary environment aliases have been removed. The only supported service entrypoint is ASGI/Hypercorn. Compiled acceptance remains subject to the step 11 normal-ABI build requirement.  

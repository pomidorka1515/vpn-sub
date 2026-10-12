# Async API conversion (Step 7)  
  
The main HTTP application now uses Quart. `api.application.create_api_application()` assembles the framework, JSON provider, template loader, one-MiB request limit, error envelopes, and ASGI proxy/access middleware independently of network startup. Admin and public APIs use `AsyncApi` and `AsyncWebApi`; subscription routing lives in `api/subscription.py`, outside core services.  
  
## HTTP contracts  
  
Routes await request parsing, rendering, file responses, repository and core calls. Authentication, validation and rate-limit decorators await their wrapped handlers and retain their signatures. Route paths, response envelopes, ordered JSON, redirects, subscription profiles, admin sessions and public `auth_token` cookies retain the contracts documented in [API.md](API.md) and [API_ADMIN.md](API_ADMIN.md). Config edits offload complete durable transactions and publish snapshots only after commit. Hashing, chart/QR rendering, file work and system sampling use the injected offload boundaries.  
  
`AsyncRateLimiter` owns a `redis.asyncio` client, verifies connectivity at startup, registers the existing sliding-window Lua script, and closes owned clients at shutdown. Counters retain route scope and client-address keys. Redis errors fail closed with HTTP 429; injected clients remain caller-owned.  
  
`ProxyMiddleware` checks the socket peer before applying forwarded headers. Only IPv4/IPv6 loopback peers are trusted. With `REQUIRE_PROXY=1`, other peers receive HTTP 400; forwarding cannot bypass that guard. Successful polling uses TRACE, other successful requests DEBUG, and errors INFO. Access records omit query strings and user-agent/header values. Hypercorn's access log is disabled in the programmatic entrypoint to avoid duplicate unredacted records.  
  
## Runtime and serving boundary  
  
`await app.create_application()` composes config/language/audit stores, native async SQLite, Redis, async panels and `AsyncSubscription`, performs recovery and attaches the runtime to `app.extensions`. A primary lock rejects a second instance. Failed startup closes resources in reverse order; runtime shutdown is idempotent.  
  
`src/asgi.py` registers Quart startup/shutdown hooks without opening runtime connections at import. `src/main.py` serves it through Hypercorn on one event loop, defaulting to `127.0.0.1:5550`. `APP_BIND` overrides the temporary `GUNICORN_BIND` fallback. The release workflow includes ASGI and Hypercorn rather than the removed WSGI entrypoint. The main binary probe imports this async stack and checks resources without needing runtime config, Redis or a panel.  
  
This is a staged API commit, not final production cutover: BWatch and Telegram polling are not yet composed into the new application (Steps 8–9). Snapshot failure status is temporarily empty. Async config reload/backup scheduling and queued event/admin-alert logging still need application lifecycle integration; shared server settings, signal/deadline verification and compiled startup/shutdown smoke checks remain for Steps 10–11. Superseded synchronous components and their dependencies remain until the final cleanup commit. Flask's transitive presence through Quart is expected.  
  
## Verification  
  
Async API tests cover admin/public authentication, cookies, user/code operations, config edits, malformed/oversized request bodies, subscriptions/builders, proxy guarding/forwarding, redacted access logging, Redis failure and factory startup unwind/repeated stop.  
  
Checks: `venv/bin/ruff check .`, `venv/bin/mypy`, `venv/bin/pyright`, and `venv/bin/pytest`: **620 passed**, with four aiohttp BasicAuth deprecation warnings. Full compiled release verification remains a later acceptance gate.  

# Development

Contributor setup, architecture, testing, code standards, and release builds for
vpn-sub. For deployment and operation, see [README.md](README.md).

## Required code standards

These rules are mandatory for all Python source files, including scripts, tests,
and package `__init__.py` files:

- **Full type hints everywhere.** Annotate function and method parameters and
  return types, as well as module variables, class attributes, and instance
  attributes. Keep callbacks and generic types fully typed too.
- **Use Python 3.12 generic syntax (PEP 695).** Write `class Example[T]:`,
  `def example[T](value: T) -> T:`, and `type Alias = ...`. Do not import
  `TypeVar`, `Generic`, or `ParamSpec`; use PEP 695 type parameters instead.
  Use built-in generics and `collections.abc` / `contextlib` ABCs rather than
  legacy `typing` aliases or `typing_extensions`.
- **No quoted type hints or annotations anywhere.** Write type expressions
  directly, including forward references; do not wrap them in strings.
- **Put `from __future__ import annotations` at the top of every Python file.**
  It must be the first import, after any shebang, encoding declaration, or module
  docstring.
- **Define `__all__` in every Python file.** List the module's intended exports;
  use an empty `__all__` when there are no public exports.
- **Use four spaces for indentation, never tabs.**

The generic syntax requirement does not lower the project's development Python
version: use Python **3.14** as configured in `pyproject.toml`.

## Checkout setup and checks

A git checkout is for development. Build the frontend as described below before
running the service or tests. Start Redis first, then:

```bash
# Python 3.14 for development
python3.14 -m venv venv
venv/bin/pip install -r requirements.txt && venv/bin/pip install -e ".[dev]"
```

After the frontend build has finished:

```bash
venv/bin/ruff check .
venv/bin/pytest
venv/bin/mypy && venv/bin/pyright
PYTHONPATH=src venv/bin/python -m main  # same entrypoint as the main binary
# Alternative checkout server (keep one worker):
PYTHONPATH=src venv/bin/hypercorn --config python:serve asgi:app
```

**Do not run backend tests, frontend tests, or release smoke tests while the
frontend is being built or rebuilt.** Wait for the build to finish before
starting tests. Stop `watch` before running tests so another rebuild cannot
overlap them. Keep build and test commands sequential, not parallel.

Ruff targets Python 3.14 and checks code style, modernization, common bugs, and
banned legacy typing imports. Run `venv/bin/ruff check . --fix` to apply safe
automatic fixes, then review the diff. CI runs `ruff check .` alongside pytest,
mypy, and pyright, plus `venv/bin/python scripts/smoke_async_release.py --checkout`
with `redis-server` installed.

### Python tests

Tests in `src/tests/` are Python packages grouped by subsystem (`api/`, `core/`,
`bwatch/`, `config/`, `db/`, bots, and runtime). Run a subsystem or a single test:

```bash
venv/bin/pytest src/tests/core
venv/bin/pytest src/tests/core/test_credentials.py::test_valid_legacy_hash_is_migrated_to_argon2
venv/bin/pytest -m redis
venv/bin/pytest -m "not integration"
```

Keep reusable factories and fakes in `tests.support`, with explicit imports such
as `from tests.support.panels import FakePanel`. Put fixtures in the nearest
subsystem's `conftest.py`; the root only owns shared DB/config/language fixtures.
The database fixture owns a per-test subscription scope, so factory-created
subscriptions are closed before SQLite, including on failure. Tests that create
their own databases must close their subscriptions explicitly or use
`subscription_scope()`. Async fixtures and tests retain function-scoped loops.
Mark tests using external services or real network transports as `integration`;
tests requiring the local Redis executable also use `redis` and skip if absent.

HTTP contracts: [docs/API.md](docs/API.md) (cookie `auth_token`) and
[docs/API_ADMIN.md](docs/API_ADMIN.md) (`Authorization` header).

## Frontend development

Use Node **24.22.2** and its bundled npm, pinned in `frontend/.node-version`.
From the repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

For iterative development, run `npm --prefix frontend run watch` separately.
Stop it before running tests, and do not start tests until any active rebuild
has finished.

Format frontend sources and tooling with `npm --prefix frontend run format`, or
check without writing changes with `npm --prefix frontend run format:check`.
Prettier uses a 120-column print width (a wrapping target, not a hard limit).
Generated files, vendor assets, fonts, and Jinja templates are excluded.

`typecheck` checks application and test TypeScript strictly without emitting files. `build` also
checks types before generating `frontend/dist/`; run it before starting Python
from a clean checkout or deploying. Do not edit or commit `frontend/dist/` or
`frontend/node_modules/`. Node and npm are build tools, not runtime dependencies.

Generated application JavaScript is minified and locally obfuscated; styles are
minified too. String literals use RC4 encoding and chained lookup aliases, including
transformed object keys. No source maps or source-path comments are emitted. Vendor assets
retain their upstream licenses. Obfuscation discourages casual inspection, but
browser code and API traffic remain inspectable: never put secrets in the frontend.

`watch` builds initially and rebuilds when frontend sources or resources change,
but does not reload the browser. It reports type errors while continuing to
rebuild, so run `build` for release verification. Restart Python after a rebuild
to refresh cached asset versions and templates; restart watch after changing
`build.mjs` or installing dependencies.

### Frontend tests

From the repository root, after the frontend build has finished and with the
frontend watcher stopped:

```sh
npm --prefix frontend test
npm --prefix frontend run test:watch
npm --prefix frontend run test:coverage
npm --prefix frontend test -- src/tests/api.test.ts
```

`test` typechecks before running Vitest once; `test:watch` runs interactively
without typechecking. `test:coverage` typechecks and generates text, HTML, and
LCOV reports in ignored `frontend/coverage/`. CI runs typechecking, tests, and
the production build.

Write typed tests in `frontend/src/tests/**/*.test.ts`, grouped by subsystem.
Import source modules directly and import `describe`, `it`, `expect`, and `vi`
from `vitest`; globals are not enabled. Tests inherit the application's strict
compiler settings. Use typed mocks such as `vi.fn<typeof fetch>()` instead of
casting fake implementations or dynamically importing bundled code.

Tests use jsdom with a stable HTTPS origin and Node's native fetch/Response
APIs. Mock requests rather than requiring a running backend. Mocks, stubbed
globals/environment variables, timers, DOM content, storage, and history are
reset between tests; clean up any listeners or other resources you create.
Pure Node tests can use a `// @vitest-environment node` comment to skip jsdom.
Test sources are excluded from production bundles and coverage reports.

## Architecture

ASGI lifespan calls `await create_application()`: primary lock, validated config snapshots,
SQLite, Redis, panel clients, `Subscription` recovery, watcher baseline/snapshots and optional Telegram bots. Startup failures unwind resources; readiness is published only after composition succeeds. Shutdown quiesces producers, settles mutations and closes resources, releasing the primary lock last.

- `src/app.py`: `create_application()` factory. Builds the `Application`
  runtime (paths, config snapshots, async DB, Redis, panels, subscription and Quart app)
- `src/asgi.py`: ASGI entrypoint (`asgi:app`); lifespan hooks start and stop the runtime
- `src/main.py`: process entry for a checkout (`python -m main`) and for the
  Nuitka binary. Runs Hypercorn in-process on one event loop
- `src/serve.py`: shared Hypercorn settings for checkout and binary serving
- `src/core/`: `Subscription` and the user, code, panel, bandwidth, and audit
  services. All user mutations go through `Subscription` to keep panels and
  SQLite consistent; the admin API, WebAPI, and Telegram bots already do, and
  Discord calls the same shared services in-process
- `src/bwatch/`: `BWatch`: owned quota, expiry, panel health and snapshot tasks
- `src/session/`: `XUiSession`, buffered aiohttp transport and generation-safe caches
- `src/config/`: atomic JSON config with thread + cross-process locking
- `src/db/`: aiosqlite repository with write transaction ownership and leased reads. Users, codes, quotas, and bandwidth live here.
  `config.json` is deployment and presentation only
- `src/api/`: Quart routes (`AsyncApi` for admin, `AsyncWebApi` for end users)
- `src/api/decorators/rate_limit.py`: local-first fixed-window limiter with
  periodic Redis reconciliation, shared by async HTTP callers
- `src/bots/telegram/`: Telegram `TelegramAdminBot` (management), `TelegramPublicBot` (user
  self-service)
- `src/bots/discord/`: optional Discord client owned by `create_application()`. One token serves public commands and
  `/admin`. See [Discord setup](src/bots/discord/README.md).
- `frontend/`: frontend sources, tooling and generated runtime resources. `docs/`: [public API](docs/API.md),
  [admin API](docs/API_ADMIN.md), [audit](docs/AUDIT.md)

### Code quality

The codebase is kept deliberately small and boring to read:

- One asyncio event loop, one serving process, one SQLite writer with explicit
  transaction ownership
- Postponed annotations everywhere; mypy and pyright run clean in CI with the
  same strict ruff set
- Constant-time comparisons for tokens and secrets, atomic config writes with
  cross-process locking, JSONL audit trail for admin actions
- pytest suite covers core and DB behavior with function-scoped event loops
  and disposable SQLite files; a release smoke script boots the binary end to
  end against loopback dependencies

## Release builds and packaging

The release binaries are Nuitka onefile builds. Build with CPython 3.14's normal
GIL ABI (empty `sys.abiflags`), not a free-threading interpreter (`python3.14t`,
`sys.abiflags == "t"`). The workflow refuses that ABI.

A tag `v*` builds frontend resources once, then compiles four binaries in parallel
on independent native GitHub-hosted runners (no QEMU/cross-compilation):

| Asset | Runner / build environment | Runtime libc floor |
| :--- | :--- | :--- |
| `vpn-sub-amd64-glibc` | `ubuntu-22.04` | glibc 2.35 |
| `vpn-sub-amd64-musl` | `ubuntu-22.04`, native Alpine 3.23 Docker | musl 1.2.5 |
| `vpn-sub-aarch64-glibc` | `ubuntu-24.04-arm` | glibc 2.39 (Ubuntu proot) |
| `vpn-sub-aarch64-musl` | `ubuntu-24.04-arm`, native Alpine 3.23 Docker | musl 1.2.5 |

Every target includes Discord. `scripts/release_shell.sh` routes common workflow
steps into the musl builder when needed; Actions/Node remain on Ubuntu. The musl
image uses normal-GIL CPython 3.14 with shared libpython, so Nuitka uses
`--static-libpython=no` there; glibc retains `--static-libpython=yes`. The image
also builds native, checksum-pinned patchelf 0.17.2: Nuitka rejects Alpine's
packaged patchelf 0.18.0. Libpython selection is
independent of the **libc** linkage: all variants dynamically use the host's libc.
Nuitka's musl loader/libc copying is explicitly disabled, and
`scripts/check_release_abi.sh` audits the bootstrap and payload architecture,
loader, and absence of bundled libc. zlib is a host dependency too. Each target
has its own ccache artifact. Publishing waits for all four builds and checks.
The release workflow's manual trigger accepts the tag/version to embed and runs
the same four builds and checks without publishing. Use it to validate packaging
before creating a release tag; only a `v*` tag push can publish a release.

`scripts/build_openssl.sh` selects `linux-x86_64` or `linux-aarch64` and uses the
native compiler/libc to build the same private, trimmed shared OpenSSL libraries
on every target. Crypto acceptance checks use actually mapped system libraries
as their comparison baseline rather than assuming glibc's `ldd` output.

Release CI verifies every Nuitka module report and runs every binary from an
unrelated working directory against disposable loopback Redis/panel
dependencies, renders bundled templates, checks SQLite integrity, sends SIGTERM
and restarts against the same state. No production credentials are required.
See [packaging checks](docs/ASYNC_PACKAGING.md).
Isolated lifecycle checks use a private mount/network namespace on glibc and a
fresh network-disabled Alpine container on musl; both install the binary under
its runtime name `vpn-sub`. The ARM glibc build targets an Ubuntu 24.04+ proot
guest, not Android/bionic; see the README's no-systemd Supervisor setup.

The unpack directory is `{CACHE_DIR}/<name>/{VERSION}/{PID}_{TIME_US}_{RANDOM}`
and is removed only after the child has exited. It is not shared across
restarts. A shared cache would be truncated by the next start while the running
process still had those `.so` files mapped.
`--onefile-child-grace-time=infinity` keeps the bootstrap from deleting that
directory during the 5s default grace window; systemd's `TimeoutStopSec` is what
bounds a stuck stop.

### Flattened Python releases

`python scripts/obfuscate_release.py src build/release-src` writes only
`build/release-src/app.min.py` (requires `python-minifier==3.4.0`). The release
workflow compiles that file with Nuitka. Internal imports and re-exports become
direct global references; stdlib and third-party imports share an import table
at the top. Automatic shortening of globals, import aliases, locals, verified
parameters, closed private methods and private classes remains enabled.
Additional identifier renames can be opted into through `--allowlist PATH` or
`--allowlist '["f:helper", "a:helper:value", "v:CONSTANT"]'`. The default is
`minifier_allowlist.json` at the repository root; an empty file or `[]` selects
no additional renames (it does not disable automatic shortening). The root must
otherwise be a JSON list of strings (not an object).
Entries use `c:` for class names, `f:` for function/method names, `a:` for individual non-variadic
parameters, and `v:` for assigned variables, followed by colon-separated lexical
scopes and the original name. For example,
`a:SomeMixin:handle:wrapper:inner:argument` selects a deeply nested parameter.
`c:ClassName` selects a class; `c:Outer:Inner` selects a nested class. Class
entries do not accept `*` and do not select methods or parameters.
Names do not include module prefixes; duplicate paths across modules are errors.
Appending `*` to an `f:` entry also selects every regular parameter of that
function/method: `f:helper*`, `f:SomeMixin:handle*`, or
`f:SomeMixin:handle:wrapper:inner*`. This renames the function/method name **and**
its positional and keyword-only parameter names, including `self`/`cls` for
methods. It does not select `*args` or `**kwargs` bindings, nested functions'
parameters, or local variables. Equivalent explicit `a:` entries are deduplicated.
All normal keyword-caller safety checks still apply; unsafe expansions error
rather than partially rewriting a signature. `*` is only allowed as this trailing
`f:` suffix.
Entries whose own names were already renamed by an earlier tracked transformation
are skipped with a warning on stderr, not renamed again. Enclosing scopes always
resolve by their original names, even after automatic or earlier allowlist
renames. For example, `f:Logger:_make_formatter` and
`c:Logger:_make_formatter:Fmt` work together in either order, including when the
private-method pass has already shortened `_make_formatter`.
Missing names without rename metadata still cause errors; short spellings alone
are not treated as evidence of mangling. Wrong types and ambiguous paths remain
errors even when an earlier transformation renamed a matching target.

The allowlist is applied after linking and the automatic private-class,
private-method and parameter passes, before python-minifier. It supplements
those systems rather than replacing them, even when nonempty. Python-minifier
still shortens eligible globals, import aliases and locals afterward. Linking
assigns collision-free aliases where required. Public class names stay intact
unless explicitly selected with `c:`, including when duplicate names require
distinct internal aliases. The CLI always prints the warning
`Renaming classes enabled. Beware of mangled log output.` on stderr, even without
`--report` or class allowlist entries, because automatic private-class renaming
remains enabled.
Known lexical references, inherited/mixin member references, direct keyword
calls, and inline literal `**kwargs` keys are rewritten together. Unknown
receivers, polymorphic overrides, external/dynamic receiver hierarchies,
escaping parameter APIs, dynamic keyword dictionaries, runtime field renames,
and unsupported bindings cause errors rather than speculative rewrites.
Use `--report` to see the resulting rename map. Dynamic reflection (including
string-based lookups), external callbacks and externally visible names remain
the allowlist author's responsibility: allowing a name is not proof that its
runtime API is private. Complete runtime field annotations are still preserved.
Builtins remain direct references (no aliases such as `A = len`) so Nuitka can
recognize and optimize their use.

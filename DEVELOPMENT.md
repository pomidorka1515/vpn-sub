# Development

Contributor setup, architecture, testing, code standards, and release builds for
vpn-sub. For deployment and operation, see [README.md](README.md).

## Required code standards

These rules are mandatory for all Python source files, including scripts, tests,
and package `__init__.py` files:

- **Commit style.** Conside, 72 chars at most. Must start with a package/module name, e.g. `docs: `, `core: ...`.
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
# Install uv first: https://docs.astral.sh/uv/getting-started/installation/
uv python install 3.14
export UV_PROJECT_ENVIRONMENT=venv
uv sync --locked
```

Use uv for all Python dependency management. Set `UV_PROJECT_ENVIRONMENT=venv`
in each checkout shell so uv uses the project's existing `venv/` directory.
`uv sync --locked` installs the runtime and development groups; `uv run --locked`
runs commands in that environment and rejects stale dependency metadata.
`pyproject.toml` is the only dependency manifest; commit `uv.lock` with it.
The native TOML lockfile includes transitive versions and artifact hashes and
must retain uv's supported multiline format.

For intentional dependency changes, edit the pins in `pyproject.toml`, run
`uv lock`, review the version changes, then run `uv sync --locked`. Do not
regenerate the lock during CI or release installation. Release builds use
`uv sync --locked --no-dev --group release --no-install-project`; their custom
Pillow rebuild uses `uv pip` with the already locked Pillow version. Keep release
commands in the synced environment so an automatic sync does not replace that
custom build with the standard wheel. CI and container builds pin uv to 0.13.10.

After the frontend build has finished:

```bash
uv run --locked ruff check .
uv run --locked pytest
uv run --locked mypy && uv run --locked pyright
PYTHONPATH=src uv run --locked python -m main  # same entrypoint as the main binary
# Alternative checkout server (keep one worker):
PYTHONPATH=src uv run --locked hypercorn --config python:serve asgi:app
```

**Do not run backend tests, frontend tests, or release smoke tests while the
frontend is being built or rebuilt.** Wait for the build to finish before
starting tests. Stop `watch` before running tests so another rebuild cannot
overlap them. Keep build and test commands sequential, not parallel.

Ruff targets Python 3.14 and checks code style, modernization, common bugs, and
banned legacy typing imports. Run `uv run --locked ruff check . --fix` to apply safe
automatic fixes, then review the diff. CI runs `ruff check .` alongside pytest,
mypy, and pyright, plus `uv run --locked python scripts/smoke_async_release.py --checkout`
with `redis-server` installed.

**Pass `--no-native-parser` to mypy when on Termux/Android** *(or LD_PRELOAD libpython)*

### Python tests

Tests in `src/tests/` are Python packages grouped by subsystem (`api/`, `core/`,
`bwatch/`, `config/`, `db/`, bots, and runtime). Run a subsystem or a single test:

```bash
uv run --locked pytest src/tests/core
uv run --locked pytest src/tests/core/test_credentials.py::test_valid_legacy_hash_is_migrated_to_argon2
uv run --locked pytest -m redis
uv run --locked pytest -m "not integration"
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
- `src/main.py`: process entry for a checkout (`uv run --locked python -m main`) and for the
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

### Release mixin architecture contract

**Do not rewrite the Discord, Telegram, database, or API mixin architecture without
updating `src/obfuscator/mixins.py` and its regression tests in the same change.**
Keep the seven composition roots (`DiscordAdminBot`, `DiscordPublicBot`,
`TelegramAdminBot`, `TelegramPublicBot`, `Database`, `AsyncApi`, and `AsyncWebApi`)
in their current modules.
Development retains the mixins; the release pipeline automatically folds each
root's complete, package-local `*Mixin` hierarchy into its concrete class.
The bot and database roots become object-based classes. API roots additionally fold
their package-local `*Routes` groups and retain `api.common.AsyncBaseApi` as their
only base, preserving its ABC metaclass, import-time route validation, constructor,
and route registration. That base must remain a direct base last in the original C3
order; other external or cross-package bases still fail the build.
New feature mixins, nested mixin inheritance, ordinary methods, and literal class
data can be added normally: compose new mixins into the existing roots. There is
no fixed list of mixin names or methods to maintain.

The feature hierarchies are closed, not runtime extension points. Do not instantiate
mixins, use them as runtime values, monkey-patch the hierarchy, or add other
consumers/subclasses of the mixins or roots. Direct immutable mixin-constant reads
(such as the backup validator's schema version) are supported. Class decorators/metaclasses,
external/dynamic/generic bases, name-mangled private members, class cells,
architecture hooks such as `__slots__`/`__init_subclass__`, hierarchy/namespace
reflection, arbitrary executable class bodies, class-local method defaults, and
unknown method decorators are unsupported and fail the release build.
Supported method decorators are builtin
`staticmethod`/`classmethod` and stdlib context managers. API methods also support
builtin getter-only `property`, the existing `api.decorators.auth` authentication
wrappers, and `api.decorators.validation` factories with literal field arguments
and builtin field types. API class data additionally supports tuples of literal
`Route(...)` declarations, starred references to other folded route tables, and
the public API's `frozenset` of literal endpoint names and route-handler generators.
These immutable table/set reads are captured at their original definition sites,
including reads outside the hierarchy such as application page classification.
Other executable class data and uncomposed route groups fail the build. Ordinary direct
`super().method(...)` calls are resolved against the original C3 order; missing,
dynamic, or consumer-dependent targets fail rather than being guessed. API calls
whose target is in the retained shared base keep normal `super()` dispatch.
Inherited field-type metadata follows the original C3 order onto concrete roots,
retaining every type alternative for later rename safety checks. Annotation hints
that name erased feature classes refer to all their concrete consumers, including
inside container and return types.

The folder emits real method definitions inside each concrete class, without
assigning function `__name__` or `__qualname__` strings. Defaults, decorator
references, and literal data are captured at their original definition sites
using collision-free helpers. Shared defaults/data retain their identities;
methods and supported decorator wrappers are distinct per concrete class.
Supported decorators are applied when the concrete class is defined. Overridden
implementations needed by `super()` become hidden concrete methods; unused
overridden methods are omitted (their default expressions are still evaluated).
Mixin classes are never reconstructed at runtime.
Run `uv run --locked pytest src/tests/obfuscator/test_mixins.py`
and the full obfuscator regression suite (`uv run --locked pytest src/tests/obfuscator`)
when changing this contract.

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

`PYTHONPATH=src uv run --locked python -m obfuscator src build/release-src` writes only
`build/release-src/app.min.py` (requires `python-minifier==3.4.0`). The release
workflow compiles that file with Nuitka. Use `-f` / `--force` to overwrite
`app.min.py` in an existing output directory; unrelated files are left intact.
Use `-d` / `--debug` to add a timestamp and original-name
comments for renamed classes, functions (including methods), arguments, and
class-scoped attributes. Variable names are omitted. Debug output adds only
comments to the normal minified output; executable text is unchanged.
The build also requires `sqlglot==30.34.0` (included in the dev dependencies).
Complete, parser-supported SQLite statements in string literals are minified:
comments and optional whitespace are removed, while quoted contents, bind
parameters, required token separators, and one original leading/trailing separator
are retained. Non-SQL strings, incomplete/unsupported SQL, and dynamic f-string
pieces are left unchanged. Queries are never executed during minification.
The build-only `src/obfuscator/` package and `src/tests/` are always excluded
from the flattened output. The package exposes `main()` and `obfuscate()`;
its CLI, pipeline, cleanup, linker, analysis, and renamers live in separate modules.
Validate generated Python before Nuitka compilation with
`uv run --locked python scripts/smoke_async_release.py --source build/app.min.py`.
This uses disposable loopback dependencies and verifies serving, bundled frontend
resources, SQLite, shutdown, and restart without using deployment state.
The CLI reports build stages and elapsed time on stderr as the build runs, ending
with `Completed in 300.32s` (the total duration, formatted to two decimal places).
Use `-v` / `--verbose` for detailed progress on stderr, including individual modules
and processing steps. This does not change the generated file; `--debug` separately
adds comments to it.
`--jobs N` controls a reusable multiprocessing executor, defaulting to half the
CPUs available to the process (rounded down, minimum one). `--jobs 1` runs without
worker processes. Parsing collects protocol inheritance edges in workers, and
the parent resolves their transitive closure without sending ASTs back for each
protocol-discovery round. Per-module cleanup and independent SQL literal
minification also run in workers; linking, mixin
folding, global renaming, and final whole-program minification remain sequential.
The pinned python-minifier's printer uses a buffered token stream in this pipeline,
without modifying its process-wide globals; its normal AST comparison remains enabled.
Review this integration and its output-equivalence tests when updating python-minifier.
The private-method and parameter passes share lexical indexes; caller safety and
inference are rechecked after method names change.
Worker count is capped at the number of input modules. The internal
`obfuscate_modules(..., jobs=N)` API accepts the same setting; multiprocessing
callers must use an importable entrypoint guarded by `if __name__ == "__main__":`.
Internal imports and re-exports become
direct global references; stdlib and third-party imports share an import table
at the top. After dead-code elimination, unused external imports are removed and
remaining imports are grouped by module. Consequently, unused imports must not be
relied on for side effects, and imported attribute lookup order can change. Dotted
submodule loads needed by a retained root import and runtime annotation dependencies
are preserved; reflective scopes conservatively retain imports. Automatic shortening of globals, import aliases, locals, verified
parameters, closed private methods and private classes remains enabled.
Additional identifier renames can be opted into through `--allowlist PATH` or
`--allowlist '["f:helper", "a:helper:value", "v:CONSTANT"]'`. The default is
`minifier_allowlist.json` at the repository root; an empty file or `[]` selects
no additional renames (it does not disable automatic shortening). The root must
otherwise be a JSON list of strings (not an object).
Source-level opt-outs are available as `from obfuscator import no_rename, no_rename_arg`.
Apply `@no_rename` to a class or regular/async function to preserve its name, or
`@no_rename_arg({"argument"})` to a function to preserve selected parameter names.
`@no_rename_arg({"*"})` preserves all parameters, including variadic bindings.
These identity decorators can be stacked with other decorators and are stripped,
along with their unused imports, before release analysis. Argument collections must
be literal lists, tuples, or sets of strings so builds never execute marker arguments.
Unknown argument names and mixing `*` with other names produce nonfatal warnings.
Overlapping allowlist rules are ignored with a warning; an overlapping `f:helper*`
rule is ignored in full. Protecting a class or function name does not implicitly
protect its methods, nested definitions, or parameters. Checkout imports of the
markers do not load the obfuscator's build dependencies.
Protections survive mixin folding; a protected overridden method that would need
a renamed hidden `super()` implementation instead fails the build.
Entries use `c:` for class names, `f:` for function/method names, `a:` for individual non-variadic
parameters, `attr:` for class-scoped attributes, and `v:` for assigned variables,
followed by colon-separated lexical scopes and the original name. For example,
`a:SomeMixin:handle:wrapper:inner:argument` selects a deeply nested parameter.
`attr:SomeClass:_internal_attribute` selects one attribute across the entire class,
not just its initializer. Nested scopes and optional file qualifiers work as usual:
`attr:models.py:Outer:Inner:_internal_attribute`. Resolved reads, writes, deletes,
class defaults, inherited references, and arbitrarily deep statically resolvable
receiver chains follow the rename. Literal builtin `getattr`, `setattr`, `delattr`,
and `hasattr` keys follow it too. Unrelated classes retain same-spelled attributes.
Unresolved/polymorphic receivers, subclass conflicts, unverifiable external inheritance,
descriptors, dataclasses, slots, dynamic reflection, namespace inspection, and
unknown external consumers fail closed. Special/name-mangled attributes are unsupported.
An inherited subclass-validation hook may inspect class names and verify class
members when the selected field exists only on instances; hooks that expose or
mutate the subclass, or have unverifiable external parent hooks, remain unsupported.
Likewise, reads with statically known names from `type(instance)` can select class
members when the selected field has no class binding; dynamic instance or class
lookups still fail.
Class defaults can contain ordinary literal containers and statically verified
non-descriptor instances, including `set()`, `WeakSet()`, `threading.Lock()`, and `threading.RLock()`;
their construction and shared identity are preserved. Empty internal `set` and
`WeakSet` registries track added receivers through aliases, copies, and ordinary
`for` loops. Their fixed `add`/`discard`/`remove`/`clear` operations are supported;
opaque consumers and unsupported operations on registries containing selected
receivers still fail closed. Empty internal `dict`, `OrderedDict`, and
`WeakKeyDictionary` caches track keys and values through subscript stores/reads
and `get`/`setdefault`/`pop`, including nested caches; unknown consumers still
reject caches containing selected receivers. Dictionary `keys()`/`values()` views
retain element flow through iteration and comprehensions; passing such views or
their receiver-containing copies to opaque consumers still fails closed.
Logger subclasses may register themselves in the
standard `logging.Logger.manager.loggerDict`; inherited logging implementations
are inspected for conflicts with the selected fields as usual.
Receiver safety follows visible function/lambda calls through positional and keyword
parameters, literal `**kwargs`, defaults, aliases, bound methods, callback forwarding,
returns, and internal instance fields. Failed argument matching on a bound callback
preserves its captured receiver when decorators are proven to retain the original
binding; uncertainty still applies to explicit arguments and unverifiable decorators.
Straight-line `if not isinstance(value, Class): raise ...` guards also establish
internal receiver families until the value is rebound. Subclass overrides remain
subject to the usual polymorphic member checks.
Local callback rebindings follow statement order and conditional branches, including
successful `try` paths whose error handlers return. Later method binding cannot flow
back into an earlier wrapper application. Stdlib `functools.wraps` copies callable
metadata without exposing the callback's argument API or changing its binding.
Recursive call graphs are solved to a fixed point; every possible callback implementation
must be visible. Known internal callees
are checked transitively rather than assumed safe. Opaque callback alternatives,
escaping callback APIs, dynamic argument expansions, and unsupported container/yield
or external storage of selected receivers fail closed. Visible callbacks
may return selected receivers through opaque external dispatchers; opting into an
attribute rename accepts external handling of those results. This does not establish
the external result's receiver type. Direct receiver arguments, bound callbacks on
selected receivers, and escapes inside visible callback bodies remain checked.
Quart's serving, request and websocket hook registrations, and `add_url_rule`
view-function registrations, are exceptions for bound callbacks: Quart stores
and invokes them without inspecting their receivers; callback bodies are still
checked. Unsupported registration methods still fail closed.
Likewise, `add_done_callback` accepts a bound callback when its receiver is traced
through single-assignment aliases to `asyncio.create_task`. This assumes the event
loop's task factory preserves the standard callback-registration behavior; custom
task factories that inspect callback receivers do not satisfy that contract.
For the supported Quart serving, request and websocket hooks, bound-method
parameters can also be renamed because Quart passes hook arguments positionally.
This does not apply to route view functions, which may receive URL keywords.
Fixed builtin protocol operations (such as `dict(tx)` and `len(tx)`) are supported
without treating them as arbitrary attribute consumers. External return annotations can establish unrelated
receivers through imported constructors/functions, method calls, assignments, and
sync/async context managers. Async function results and annotated awaitables are
unwrapped at `await`; generic context-manager parameters and `Self` are retained.
Custom external awaitables can also declare their result as the return parameter
of an annotated `Generator` returned by `__await__`; unknown results remain unknown.
Annotations are read as syntax, not evaluated, and external APIs are never called
to infer their results. Missing or unsupported annotations and unknown union
alternatives still fail closed for same-spelled fields. Ordinary exception handlers
also establish external receiver types from resolved exception classes, including
import aliases and literal exception tuples. Sibling handlers may reuse a target
name; competing assignments, unresolved alternatives, and `except*` groups do not
establish a receiver type.
Shared inference follows imported and re-exported type aliases across modules,
including nested container shapes. Cyclic or unresolved alias alternatives retain
uncertainty instead of proving an unrelated receiver. Receiver errors retain
original source line numbers after mixin methods are relocated.
Straight-line self-rebindings read the incoming parameter/assignment hints on
their right-hand side, rather than recursively depending on their own store.
Optional `None` alternatives do not invent unknown ordinary-attribute receivers.
Starred list/tuple/set construction retains element flow without inventing fixed
positions after an expansion. Annotation-only class tables are declarations, not
additional opaque runtime values alongside their concrete subclass tables.
Shared receiver inference composes container shapes through builtin conversions,
`zip`/`enumerate`, `next`, mapping views, and tuple slices. Async calls retain an
awaitable layer; `asyncio.gather` (including starred iterable inputs), task creation,
`shield`, and `wait_for` preserve their result shapes. Standard-operation rules
use resolved import/builtin bindings, not identifier spellings. PEP 695 generic
function parameters and ordinary generic class fields/methods are instantiated
from visible argument shapes, including generic constructor arguments. Annotated
callable return shapes and visible function or verified method references also let
generic callback dispatchers retain their result receivers. These are syntax-only substitutions,
not a replacement for a type checker; unsupported variadic generics and
unresolved substitutions do not establish receiver types. Generic calls bind
defaults, positional-only/keyword-only arguments, fixed tuple expansions, literal
keyword dictionaries, and ordinary variadic arguments before substituting return
shapes. Unknown expansions and invalid bindings cannot prove a receiver type.
Method lookup on a bounded type parameter follows its complete internal class family,
including concrete consumers of folded mixins. Unresolved bounds and competing
method implementations remain errors.
Visible custom containers use annotated `__iter__`/`__next__`, async iteration,
and `__getitem__` protocols; generic arguments are projected through visible
parameterized base classes rather than adding each container to a name table.
Opaque protocol alternatives remain uncertain. Runtime expression joins retain
unknown alternatives, and different-length tuple alternatives are widened rather
than inventing fixed element positions. Binding annotations
survive verified imported-factory reassignments and nested cleanup paths, including
methods on statically resolved external instances. Callable bindings and receiver
classes are inspected without invoking factories; opaque
callback rebindings still fail closed, and known conflicting assignment types
are retained rather than hidden by the declaration.
Explicit `typing.cast` shapes survive removal of the runtime cast. Captured
builtin aliases (including folded static/class method decorators) retain their
binding semantics. External Python factories without return annotations can
supply closed constructor/local/tuple return shapes from inspected source.
Native functions can use the stdlib stubs bundled with the pinned development
`mypy` dependency: stubs are parsed, not imported, and every overload compatible
with the supplied argument names/count must have a known result. Missing stubs,
unsupported generic returns, recursive or opaque source paths remain unknown.
For external methods, statically proven literal arguments can select a stub
overload in declaration order; native inherited method and context-manager results
also follow stub annotations. Unmatched or uncertain alternatives are not guessed.
Transparent `contextlib` closing/null contexts preserve their wrapped type.
External bases are resolved through installed imports (including aliases and generic
subscriptions) without constructing instances or evaluating base expressions. Their
inherited Python implementations and receiver helpers are inspected for member/field
conflicts. Unrelated members are eligible; inherited overrides, opaque implementations,
custom metaclasses, dynamic receiver hooks, and unverifiable reflection still fail closed.
Attribute selections cannot overlap a `v:` selection for the same class binding.
Constructor parameters use the existing parameter syntax, such as
`a:SomeClass:__init__:argument`; `__init__` itself must retain its special name.
Positional-only receiver parameters are supported. Constructor keyword calls and
inline literal keyword dictionaries follow parameter renames; reflected or
escaping constructor APIs are rejected for explicit selections.
`c:ClassName` selects a class; `c:Outer:Inner` selects a nested class. Class
entries do not accept `*` and do not select methods or parameters.
An optional source-relative file path can appear immediately after any kind prefix:
`f:db/backup.py:do_backup`, `c:models.py:Outer:Inner`,
`a:db/backup.py:do_backup:path`, or `v:config/backup.py:do_backup:content`.
Paths must end in `.py`, use `/`, and omit the source root (`src/`). Package
initializers use `package/__init__.py`. The file qualifier must come first;
lexical scopes afterward retain arbitrary nesting depth and `f:` still supports
the trailing `*`. Unqualified duplicate paths across modules remain errors.
Qualified selections rename linker collision aliases normally, rather than
preserving generated `_g…` names. All existing rename safety checks still apply.
Methods need their declaring class scope: `_auth_save` has the lexical path
`f:AsyncAuthRoutes:_auth_save`, not `f:_auth_save`. Missing-scope errors suggest
matching full lexical paths, but do not automatically select them or bypass safety
checks. Functions with duplicate module-level names, such as `do_backup`, require
file-qualified selectors to select them individually.
Builtin properties use the same function selectors: `f:Record:value` renames a
getter and its same-name `@value.getter`, `@value.setter`, and `@value.deleter`
chain together, including inherited reads, writes, and deletes. Use
`f:Record:value*` to also rename regular parameters in every accessor, or
`a:Record:value:new_value` to select a setter parameter individually. A parameter
name shared by accessors (such as `self`) is renamed consistently in all of them.
Parameter selections reject exposed class-level property descriptors, whose
accessor functions could be called with external keyword arguments. Custom
property decorators and unrelated duplicate method definitions remain unsupported.
Properties use `f:` rather than `attr:` selectors.
Appending `*` to an `f:` entry also selects every regular parameter of that
function/method: `f:helper*`, `f:SomeMixin:handle*`, or
`f:SomeMixin:handle:wrapper:inner*`. This renames the function/method name **and**
its positional and keyword-only parameter names, including `self`/`cls` for
methods. It does not select `*args` or `**kwargs` bindings, nested functions'
parameters, or local variables. Equivalent explicit `a:` entries are deduplicated.
All normal keyword-caller safety checks still apply; unsafe expansions error
rather than partially rewriting a signature. `*` is only allowed as this trailing
`f:` suffix. Dunder function/method names are always preserved: an entry such as
`f:SomeClass:__init__` is ignored with a warning, while
`f:SomeClass:__init__*` selects only its regular parameters and retains the name.
For callbacks passed to external libraries, select only the function name when
appropriate: `f:reject_nonfinite_constant` works for the JSON parser callback,
while `f:reject_nonfinite_constant*` fails the escaping-parameter safety check.
Entries whose own names were already renamed by an earlier tracked transformation
are skipped with a warning on stderr, not renamed again. Enclosing scopes always
resolve by their original names, even after automatic or earlier allowlist
renames. For example, `f:Logger:_make_formatter` and
`c:Logger:_make_formatter:Fmt` work together in either order, including when the
private-method pass has already shortened `_make_formatter`.
Missing names without rename metadata still cause errors; short spellings alone
are not treated as evidence of mangling. Wrong types and ambiguous paths remain
errors even when an earlier transformation renamed a matching target.

After linking and mixin folding, a whole-program dead code pass removes unused
functions and closed class methods, including unreachable recursive call chains
and nested helpers. Callback references and aliases count as uses. Builtin
static/class methods and imported stdlib context-manager wrappers can be removed
too. Inherited methods are checked individually: inspectable external base
implementations retain overrides and members used by inherited code, while new
methods without attribute or string references can be removed. Opaque external
consumers, dynamic receiver access, unknown decorators, implicit special methods,
and defaults whose evaluation might have effects preserve definitions.
Allowlist entries for removed symbols (including their parameters and nested
definitions) are skipped with a `dead code removed` warning; ambiguous selectors
and incorrect selector types still fail. This pass only changes flattened builds;
the standalone single-module API retains potentially external entry points.

The allowlist is applied after linking and the automatic private-class,
private-method and parameter passes, before python-minifier. It supplements
those systems rather than replacing them, even when nonempty. A non-mutating
preflight after dead-code elimination checks lexical selectors, definite escaping
function parameters (including lexical aliases), and dynamic keyword expansions
before dataclass and automatic-renaming analysis. Attribute class selectors are
also resolved before building the receiver-flow graph. Checks that depend on
later transformations remain at the original allowlist stage, preserving generated
names, skip warnings, and output. Inheritance/member lookups are cached within
each analysis pass and invalidated before the parameter pass after method renames.
Dead-code analysis shares receiver classifications and escape checks across new
methods of the same inherited class, while checking inherited conflicts separately
for each method. Its external annotation lookups do not construct the attribute-flow
graph. Attribute allowlisting indexes executable expressions once for family checks,
deduplicates flow dependencies, and reads cached external syntax without copying its
analysis metadata. Receiver guard analysis indexes candidate bindings once per
function and caches lexical results across flow-state updates, avoiding repeated
statement walks for unguarded names. Python-minifier still shortens eligible globals,
import aliases and locals afterward. After packing and the tracked rename passes,
a pre-minifier pass evaluates short module-level
string, integer, and float constants, substitutes their literal values at lexical
references, and deletes their assignments. The value's text must be strictly
shorter than 150 characters (string contents, or `str(value)` for numbers).
Constant arithmetic, concatenation, references to earlier eligible constants,
and simple constant f-strings are supported without executing application code.
Only unconditional, single-assignment module bindings qualify: class/function
locals, rebound names, forward reads, quoted-annotation dependencies, special
dunder globals, and reflective scopes are preserved. Calls, mutable values,
nonfinite floats, and unsupported or unbounded expressions remain runtime code.
The following pre-minifier layer consolidates repeated
single-assignment module aliases of builtins, imported stdlib bindings, and
`None`/boolean/ellipsis singletons into one alias per value after the import table.
References and runtime field annotations follow the shared alias. Calls such as
`object()` and mutable literals are never shared; conditional, local, rebound,
and third-party aliases are left alone. Import bindings are separately shortened
so unrelated preserved parameter or field names cannot keep `import xyz as xyz`.
Reflective scopes retain the existing conservative rename restrictions.
Linking assigns collision-free aliases where required. Class names used only in
direct constructor calls are shortened automatically, regardless of their spelling.
Class-object escapes, inheritance, unknown decorators, reflection, and retained
runtime annotation dependencies preserve names, as do name-mangled members,
parameters, and slots. Explicitly renamed classes are not renamed a second time.
Other public class names stay intact
unless explicitly selected with `c:`, including when duplicate names require
distinct internal aliases. The CLI always prints the warning
`Renaming classes enabled. Beware of mangled log output.` on stderr, even without
`--report` or class allowlist entries, because automatic private-class renaming
remains enabled.
Mixin folding runs before method/parameter renaming and the allowlist, so safety
analysis sees real concrete methods, including lowered `super()` dispatch.
For the seven folded roots, selectors use the concrete class rather than the
declaring mixin: `f:Database:create_user` renames the method, while
`f:Database:create_user*` also selects its regular parameters. A shared mixin
method copied into two roots is selected independently on each concrete class.
Former mixin- or route-group-scoped selectors for these hierarchies must be updated; mixin
classes no longer exist at this stage. Other hierarchies retain their existing
lexical selectors. Only flattened release output is changed; checkout imports
and source classes are untouched.
Known lexical references, inherited/mixin member references, direct keyword
calls, and inline literal `**kwargs` keys are rewritten together. Unknown
receivers, polymorphic overrides, external/dynamic receiver hierarchies,
escaping parameter APIs, dynamic keyword dictionaries, runtime field renames,
and unsupported bindings cause errors rather than speculative rewrites.
For method names, visible callers can resolve a receiver even when its protocol
annotation was stripped, including values passed through supported dataclass
constructors into fields. External receiver hints also follow straight-line local
rebindings and imported unions of `TypedDict` types (for example, `scope = scope.copy()`).
Unrelated same-named methods are left alone; a call site that can dispatch to both
a selected method and another method still fails.
Quoted runtime type references also prevent selecting names they depend on;
strings used as `typing.Literal` values or `typing.Annotated` metadata are data,
not type references. In particular, `Literal["ok"]` does not block `f:ok`.
Use `--report` to see the resulting rename map. Dynamic reflection (apart from
verified literal attribute lookups), external callbacks and externally visible
names remain the allowlist author's responsibility: allowing a name is not proof that its
runtime API is private. A whole-program dataclass pass replaces ordinary field
annotations with `0` only for proven-closed Python-constructed records. Dacite
schemas and their transitive field/container dependencies retain their types.
Fields are also shortened when their names are private: resolved attribute chains,
constructor keywords, and inline literal `**kwargs` keys are rewritten together;
positional argument order is unchanged. `asdict()` preserves names recursively,
without preventing annotation stripping. Unresolved `asdict()` operands do not
preserve unrelated records; untracked record flows retain their APIs at the
point where they escape. Internal container copies, append/extend operations,
iteration, and consistently typed reassignments remain tracked, including through
inherited internal constructors. Internal inheritance alone does not expose the
dataclasses mentioned in method signatures. Unrelated opaque attribute receivers
do not block closed records by spelling; actual call arguments must flow into
matching typed parameters. Escapes, reflection, dataclass inheritance,
dataclass directives (`ClassVar`, `InitVar`, `KW_ONLY`), unknown descriptors,
quoted annotations, and unsupported receivers preserve the relevant APIs rather
than guessing. Dynamic constructor keywords preserve field names. Definitions
without visible Python constructors are retained as possible external schemas.
Other runtime field annotations are preserved, but `Literal`
values are widened to their types before typing metadata is removed: for example,
`Literal["ok", "failed"]` becomes `str`, and `Literal[True, 0]` becomes `bool | int`.
Direct builtin references remain direct (no new aliases such as `A = len`) so
Nuitka can recognize and optimize their use. Existing repeated builtin aliases
are consolidated, not introduced for ordinary builtin calls.

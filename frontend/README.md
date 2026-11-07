# Frontend tooling

Use Node **24.22.2** (with its bundled npm), pinned in `.node-version` and read
by CI. Node/npm are development tools only, never Python runtime requirements.
TypeScript and esbuild are the only direct dependencies, both development-only;
`package-lock.json` pins the dependency tree.

From the repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run watch
```

`typecheck` runs strict TypeScript with no output, unchecked indexed access and
exact optional properties. `build` checks types first and exits nonzero on errors
before touching existing output. esbuild alone is not a type checker.

## Incremental migration status

Part 3 establishes the complete runtime tree. `src/` owns all frontend resources;
the former root `res/` is removed, with no compatibility copies or symlinks.
Python consumes **only `frontend/dist/`**, resolved through `bundled_root()`.
Build explicitly before running Python from a clean checkout. Missing output
raises a setup error with the install/build commands; startup never runs npm.
CI builds before pytest/smoke, and release builds before font checks/packaging.
Nuitka includes only `frontend/dist/`, not sources or development dependencies.

| Source | Generated runtime output |
| --- | --- |
| `src/templates/` | `dist/templates/` (unchanged Jinja, including inline scripts/styles) |
| `src/styles/` | `dist/assets/` (byte-preserving, no CSS rewriting) |
| `src/fonts/` | `dist/fonts/` (including server-only TTF and licenses) |
| `src/vendor/` | `dist/vendor/` (unchanged distributions and licenses) |
| `src/entries/common.ts` and `src/shared/*.ts` | `dist/assets/common.js` (blocking, isolated IIFE) |
| `src/entries/{auth,admin-auth,dashboard,history}.ts` | `dist/assets/{auth,admin-auth,dashboard,history}.js` |
| `src/entries/admin.ts` and its imports | `dist/assets/admin/main.js` |
| `src/entries/{browser,redirect,error}.ts`, when present | `dist/assets/{browser,redirect,error}.js` |

Entry points are explicit in `build.mjs`; no code splitting, manifest, hashed
filenames or source maps. Python computes content-derived `?v=` versions over the
complete output tree. Existing public asset URLs remain unchanged and allowlisted;
templates keep their exact CSS/script order and prefix-aware URLs. Font CSS keeps
its runtime substitution markers. Chart.js and CodeMirror remain lazy local loads
with unchanged vendor bytes; the editor's `import.meta.url` still resolves from
`/admin/main.js` to `/vendor/`, retaining its version query. Internal admin modules
are bundled and are no longer individually served.

The five typed page entries are required; the explicitly listed browser, redirect
and error entries are included only when their source files exist. Watch checks for
those optional entries on every rebuild. Page bundles retain ESM output without
tree shaking or identifier minification. The ambient CSS declaration supports
future imports; there is deliberately no frontend test command or replacement
suite yet.

Only `frontend/dist/` is build-owned and cleaned; never edit or commit it or
`node_modules/`. All copy inputs are required: a missing resource directory fails
the build rather than silently emitting an incomplete tree. Paths are relative
to the tooling, not the caller's working directory.

## Watch mode

Watch performs an initial build and debounces/serializes full rebuilds on source
changes, including additions and deletions of templates, styles, fonts and vendor
files. This updates the output used by Quart; it does not reload the browser.
Restart Python after rebuilding to refresh its process-cached asset/font versions
and Jinja templates.
Restart watch after changing `build.mjs` or installing dependencies.

A parallel `tsc --watch` reports ongoing type errors in the same terminal.
Development resources still rebuild during semantic errors; syntax/bundle errors
are logged and watch stays alive for corrections. **Watch output is not release
verification**: run `build` before deployment. Stop with Ctrl-C; the type-checker
child is also stopped.

## Browser compatibility

No explicit browser-version policy was found in the existing frontend. It already
uses native modules, optional chaining, BigInt, `Object.hasOwn`, `inert`,
AbortController and streamed fetch. ES2020 is the conservative emitted syntax
target (including BigInt); ES2022/DOM type libraries describe existing APIs, not
an increased syntax target. There are no polyfills or changed browser API
requirements. This does not establish support for older browsers or replace
browser checks during page migration.

## Resource footprint

On Linux x64 with the pinned Node toolchain, `npm ci` installs 3 packages
(TypeScript, esbuild and its platform binary), occupying **34 MiB** on disk.
The lockfile also records optional esbuild binaries for other platforms; they
are not all installed. Part 3 emits **30 files / 1,054,109 bytes**, compared with
the old **53 files / 1,058,147 bytes**. Of these, 26 copied resources total
**837,205 bytes** and retain their original SHA-256 hashes. The four JS bundles
total **216,904 bytes** (uncompressed), with no runtime npm dependencies.

These build/resource checks are not frontend test coverage or proof of visual
or browser performance parity. Browser and actual frozen-binary verification
remain unverified locally; release retains its isolated binary smoke checks.

## Part 3 verification (2026-11-07)

- All 53 original resources were accounted for and moved without byte changes;
  all 26 copied output resources match their original SHA-256 hashes.
- A staged clean checkout, without `dist/`, fails `--probe` with the explicit
  setup commands. Installation/build from outside that checkout succeeds, then
  its checkout `--probe` succeeds with Node excluded from `PATH`.
- Repeated production builds have identical output hashes and remove stale
  output. Type errors reject production builds without changing prior output.
  Watch additions, edits and deletions of templates/styles preserve their bytes
  and Jinja expressions. All emitted first-party JS passes Node syntax checks;
  no maps, TypeScript or tool/dependency files are emitted.
- Existing dashboard auth checks also pass against the **generated** classic
  scripts (5 checks), including action-local wrong-password errors and ordinary
  unauthorized redirects. The two old scripts remain until plan step 9; only
  their source-reading paths changed here. No replacement suite was added.
- Isolated Quart checks passed for both EN/RU at root and `/nested/sub`, all
  served asset URLs, redirect and browser 404. All 20 page bodies match the
  previous commit after normalizing cache versions, including byte counts.
- `npm ci`, typecheck, build, Ruff, mypy, Pyright, **2002 pytest tests**, and the
  checkout lifecycle smoke passed. The first concurrent run had a Redis socket
  timeout and a Pyright timeout; sequential reruns passed without code changes.

Bundle sizes (uncompressed bytes): common **10,571**, dashboard **15,312**,
history **10,516**, admin **180,505**. The source-derived admin cold static request
budget falls from **29 to 6**, excluding fonts, API and document requests; vendor
JS remains lazy. This is not a measured browser network trace.

Two same-environment in-process HTTP runs used isolated temporary SQLite, fake
panels/Redis, actual language tables, and 30 warm requests per page/language.
The table aggregates the EN/RU warm medians across both runs (milliseconds):

| Page | Root before → after | Nested before → after |
| --- | --- | --- |
| Auth | 2.321 → 2.795 | 2.478 → 2.857 |
| Admin login | 2.594 → 3.264 | 3.053 → 3.135 |
| Dashboard | 4.215 → 3.868 | 4.210 → 4.465 |
| History | 3.294 → 3.868 | 3.877 → 4.470 |
| Admin | 7.653 → 5.422 | 6.518 → 6.177 |

Sub-millisecond warm variations and noisy cold samples do not establish browser
startup or interaction performance. No browser or Nuitka executable is available
locally; visual, theme-paint, interaction/timer cleanup and actual frozen payload
execution remain for the subsequent migration verification. Release now compares
packaged frontend bytes with the built tree and rejects source/maps/tools, while
retaining its existing outside-checkout binary/lifecycle checks.

## Part 4: typed shared logic

`src/shared/` now owns API transport, bootstrap validation, EN/RU language and
cookie/storage handling, themes, formatting, escaped text, password visibility,
busy buttons, keyed stale-response protection, notifications and dialogs. Imports
do not inspect the DOM, read storage, start timers or issue requests. Factories
take the document, storage, navigation, fetch, translation or clock dependencies
they need. Dialogs remove listeners on close/dispose and restore focus; theme
subscribers receive an unsubscribe function. Admin charts subscribe to theme
changes instead of replacing the global theme implementation.

`src/templates/shared-bootstrap.html` emits a single `application/json` object
with Jinja `tojson`: deployment prefix, selected/fallback language tables and
page configuration. `readBootstrap` validates it before use. No server-provided
value is interpolated into an executable JavaScript string on these five pages.
The blocking common entry applies the saved theme in the head, before CSS and
visible content, including when browser storage is denied.

The **only shared compatibility adapter** is `src/entries/common.ts`: it imports
typed implementations and exposes their existing names to the not-yet-migrated
classic pages/inline handlers. This is not a second implementation or a global
dependency inside shared modules. The typed API configuration has one owner;
its adapter getter/setter preserves the legacy admin SSE consumer. Remove this
adapter as part 6 replaces the remaining page consumers with explicit imports.
The auth/page scripts, inline handlers, 404 and redirect scripts remain in their
existing page migration scope; no replacement frontend suite/dependency was added.

API responses enter as `unknown`. The common boundary checks object envelopes,
boolean `success` and string `msg`; page-specific `obj` remains `unknown`, to be
validated by each typed page as it migrates. Malformed envelopes produce the
existing invalid-response failure shape. Transport preserves 204, non-enumerable
`rawJson`, ETags, the `Symbol.for('api.serialize')` hook (including exact large
integers), authorization guards, network throw/toast policy and action-local 401
overrides. Raw configuration text is not round-tripped through native JSON.

### Verification (2026-11-07)

- Reproducible `npm ci`, strict typecheck and production build pass. Dependency
  footprint is unchanged: 3 installed packages, **34 MiB**, no runtime packages.
- Ruff (including formatting), mypy, Pyright and **2008 backend tests** pass;
  six new backend cases verify JSON-only bootstrap escaping/round trips for EN/RU,
  root/nested prefixes and script-breaking strings. Existing page tests verify
  bootstrap-before-common-before-CSS ordering. The first run caught a corrected
  page-specific-table assertion; a later run exhausted `/tmp`. The full successful
  rerun used a private checkout-local temporary directory, subsequently removed.
- The existing dashboard auth script passes all five checks against generated
  common output. Both obsolete scripts are preserved for their planned removal.
- Ad-hoc checks outside the repository verify side-effect-free imports, invalid
  bootstrap/envelope rejection, raw large integers, serialization hooks, 204,
  ETags, local versus redirecting 401s, network policies, busy nesting/recovery,
  keyed stale replies, escaping, formatting, themes, EN/RU fallbacks, language
  cookies/navigation, dialog Enter/Escape/backdrop, replacement and focus cleanup.
  Admin logout checks also verify session-bound CSRF, token acquisition before
  bootstrap, failure warnings and redirect only after confirmed session expiry.
  These checks are not a committed frontend test suite or a coverage claim.
- Isolated HTTP checks pass for all five pages, both languages, root and nested
  prefixes, all existing asset routes, redirect and browser 404. The checkout
  lifecycle smoke passes, including Redis/SQLite, SIGTERM and restart. No backend
  runtime path, public route or packaging integration changes were required.
- Generated output is **31 files / 1,060,434 bytes**. Common is **17,588 bytes**,
  dashboard **15,312**,
  history **10,516**, admin **180,560**; the extra file is the Jinja include.
  Shared logic remains a single common request; CSS/fonts/vendor files and lazy
  library loading are unchanged.

Sequential same-environment HTTP runs against the previous commit and part 4,
using isolated data and 30 warm requests per page/language/prefix, gave aggregate
warm medians (milliseconds): auth **3.027 → 2.990**, admin login **2.756 → 3.766**,
dashboard **4.666 → 4.914**, history **3.913 → 3.911**, admin **5.814 → 5.832**.
An initial run concurrent with other verification was much noisier and was not
used for comparison. These server timings and unchanged static request counts
do not establish browser startup/interaction parity. No browser or frozen binary
execution was available: visual theme-paint, real interactions and frozen checks
remain explicitly unverified, rather than inferred from TypeScript compilation.

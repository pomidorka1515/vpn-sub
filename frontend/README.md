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
| `src/legacy/{common,dashboard,history}.js` | `dist/assets/{common,dashboard,history}.js` |
| `src/legacy/admin/main.js` and its imports | `dist/assets/admin/main.js` |

Entry points are explicit in `build.mjs`; no code splitting, manifest, hashed
filenames or source maps. Python computes content-derived `?v=` versions over the
complete output tree. Existing public asset URLs remain unchanged and allowlisted;
templates keep their exact CSS/script order and prefix-aware URLs. Font CSS keeps
its runtime substitution markers. Chart.js and CodeMirror remain lazy local loads
with unchanged vendor bytes; the editor's `import.meta.url` still resolves from
`/admin/main.js` to `/vendor/`, retaining its version query. Internal admin modules
are bundled and are no longer individually served.

`src/legacy/` is a temporary home for **unconverted first-party JavaScript**, not
vendor code. Typed shared/page migration belongs to subsequent plan steps. Classic
scripts are emitted without wrapping, tree shaking or identifier minification so
inline handlers keep their globals. The admin entry remains a native module.
Auth/redirect/error scripts stay in their unchanged templates until page migration;
the scriptless browser template needs no entry. Register typed replacements in the
same explicit list when ready. The ambient CSS declaration supports future imports;
there is deliberately no frontend test command or replacement suite yet.

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

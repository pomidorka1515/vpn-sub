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

This is step 2 tooling, **not a migrated runtime resource tree**. Python still
serves `res/`; no resources have moved and no page entry points are registered.
The initial build therefore produces **0 files / 0 bytes**. The ambient CSS module
declaration supports future side-effect stylesheet imports; it is not application
logic or frontend test coverage. There is deliberately no test command yet.

As pages migrate, register their entry points explicitly in `build.mjs`.
Bundles use stable names under `dist/assets/`, ESM without splitting or source
maps. The build copies existing `src/templates/`, `src/styles/`, `src/fonts/` and
`src/vendor/` into `dist/templates/`, `dist/assets/styles/`, `dist/fonts/` and
`dist/vendor/` respectively, preserving bytes (including Jinja and licenses).
These directories are optional during migration. CSS ordering, relative URLs,
and template asset references must be reconciled when resources move in step 3.
Only `frontend/dist/` is cleaned. Never edit or commit it or `node_modules/`.
All paths are resolved relative to this tooling, not the caller's directory.

## Watch mode

Watch performs an initial build and debounces/serializes full rebuilds on source
changes, including additions and deletions of templates, styles, fonts and vendor
files. This prepares the output needed by Quart once Python integration migrates;
it does not reload the browser or change Python's resource root today.
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

## Step 2 verification and footprint

On Linux x64 with the pinned Node toolchain, `npm ci` installs 3 packages
(TypeScript, esbuild and its platform binary), occupying **34 MiB** on disk.
The lockfile also records optional esbuild binaries for other platforms; they
are not all installed. Generated resources currently total **0 bytes / 0 files**
(the empty output directory itself occupies 4 KiB on this filesystem).

Installation, type checking and production build passed. Temporary isolated
fixtures verified byte-preserving template/style/font/license copies, JS/CSS
bundling, identical output hashes on repeated builds, no source maps, execution
outside the repository directory, type-error rejection without changing prior
output, and watch additions/edits/deletions/type-error reporting/recovery/shutdown.
These tooling checks are not a replacement frontend test suite. Browser,
performance, Python resource integration and frozen release checks remain for
later migration steps; this step changes none of those runtime consumers.

import { spawn, spawnSync } from "node:child_process";
import { watch } from "node:fs";
import { cp, mkdir, readdir, rm, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = dirname(fileURLToPath(import.meta.url));
const source = join(root, "src");
const output = join(root, "dist");
const tsc = join(root, "node_modules/typescript/bin/tsc");
const watching = process.argv.includes("--watch");
if (process.argv.slice(2).some((arg) => arg !== "--watch")) {
  throw new Error("Usage: node build.mjs [--watch]");
}

// Transitional JavaScript entries retain their current script loading model.
// Replace these paths with typed entries as pages migrate, not directory globs.
const classicEntries = {
  dashboard: "src/legacy/dashboard.js",
  history: "src/legacy/history.js",
};
const moduleEntries = { "admin/main": "src/legacy/admin/main.js" };
const copies = [
  ["templates", "templates"],
  ["styles", "assets"],
  ["fonts", "fonts"],
  ["vendor", "vendor"],
];

async function size(path) {
  let files = 0;
  let bytes = 0;
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const child = join(path, entry.name);
    if (entry.isDirectory()) {
      const nested = await size(child);
      files += nested.files;
      bytes += nested.bytes;
    } else {
      files += 1;
      bytes += (await stat(child)).size;
    }
  }
  return { files, bytes };
}

async function rebuild() {
  // Only this generated directory is build-owned. Never clean source files.
  await rm(output, { recursive: true, force: true });
  await mkdir(output, { recursive: true });
  // Isolate shared internals from the classic pages' top-level declarations.
  await build({
    absWorkingDir: root,
    entryPoints: { common: "src/entries/common.ts" },
    outdir: join(output, "assets"),
    bundle: true,
    format: "iife",
    platform: "browser",
    target: "es2020",
    sourcemap: false,
    legalComments: "eof",
    logLevel: "info",
  });
  for (const entryPoints of [classicEntries, moduleEntries]) {
    await build({
      absWorkingDir: root,
      entryPoints,
      outdir: join(output, "assets"),
      bundle: true,
      splitting: false,
      format: "esm",
      platform: "browser",
      target: "es2020",
      // Classic pages still call top-level functions from inline handlers.
      // ESM output without imports/exports is also valid as a classic script;
      // do not wrap, shake or rename those globals before their typed migration.
      treeShaking: false,
      minify: false,
      sourcemap: false,
      legalComments: "eof",
      logLevel: "info",
    });
  }
  for (const [from, to] of copies) {
    await cp(join(source, from), join(output, to), { recursive: true });
  }
  const { files, bytes } = await size(output);
  console.log(`Built frontend/dist: ${files} files, ${bytes} bytes`);
}

if (!watching) {
  // esbuild strips types but does not check them. Do not touch output on failure.
  const checked = spawnSync(process.execPath, [tsc, "--project", "tsconfig.json"], {
    cwd: root,
    stdio: "inherit",
  });
  if (checked.error) throw checked.error;
  if (checked.status !== 0) process.exit(checked.status ?? 1);
  await rebuild();
} else {
  // Report ongoing semantic errors independently; watch output is not a release.
  const checker = spawn(
    process.execPath,
    [tsc, "--project", "tsconfig.json", "--watch", "--preserveWatchOutput"],
    { cwd: root, stdio: "inherit" },
  );
  let running = false;
  let pending = false;
  let timer;
  async function drain() {
    pending = true;
    if (running) return;
    running = true;
    try {
      while (pending) {
        pending = false;
        try {
          await rebuild();
        } catch (error) {
          console.error("Frontend rebuild failed; waiting for changes:", error);
        }
      }
    } finally {
      running = false;
    }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(drain, 100);
  }
  const watchers = [
    watch(source, { recursive: true }, schedule),
    watch(join(root, "tsconfig.json"), schedule),
  ];
  function stop(code) {
    clearTimeout(timer);
    for (const watcher of watchers) watcher.close();
    checker.kill();
    process.exit(code);
  }
  checker.on("error", (error) => {
    console.error(error);
    stop(1);
  });
  checker.on("exit", (code) => stop(code ?? 1));
  for (const watcher of watchers) {
    watcher.on("error", (error) => {
      console.error(error);
      stop(1);
    });
  }
  process.on("SIGINT", () => stop(0));
  process.on("SIGTERM", () => stop(0));
  await drain();
  console.log(
    "Watching frontend/src and tsconfig.json; restart after editing build.mjs.",
  );
}

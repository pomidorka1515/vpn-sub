import { spawn, spawnSync } from "node:child_process";
import { existsSync, watch } from "node:fs";
import { cp, mkdir, readdir, rm, stat, writeFile } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import JavaScriptObfuscator from "javascript-obfuscator";
import { esbuildLang } from "./lang-plugin.ts";

const root = dirname(fileURLToPath(import.meta.url));
const source = join(root, "src");
const output = join(root, "dist");
const tsc = join(root, "node_modules/typescript/bin/tsc");
const watching = process.argv.includes("--watch");
if (process.argv.slice(2).some((arg) => arg !== "--watch")) {
  throw new Error("Usage: node build.mjs [--watch]");
}

const moduleEntries = {
  auth: "src/entries/auth.ts",
  reset: "src/entries/reset.ts",
  "admin-auth": "src/entries/admin-auth.ts",
  "admin/main": "src/entries/admin.ts",
};
const classicEntries = {
  dashboard: "src/entries/dashboard.ts",
  history: "src/entries/history.ts",
};
const optionalModuleEntries = {
  browser: "src/entries/browser.ts",
  redirect: "src/entries/redirect.ts",
};
const optionalClassicEntries = {
  error: "src/entries/error.ts",
};
const copies = [
  ["templates", "templates"],
  ["fonts", "fonts"],
  ["vendor", "vendor"],
];

async function buildJavaScript(options) {
  const result = await build({
    ...options,
    bundle: true,
    platform: "browser",
    target: "es2020",
    treeShaking: true,
    minify: true,
    sourcemap: false,
    legalComments: "none",
    write: false,
    logLevel: "info",
  });
  for (const file of result.outputFiles) {
    const obfuscated = JavaScriptObfuscator.obfuscate(file.text, {
      compact: true,
      controlFlowFlattening: true,
      controlFlowFlatteningThreshold: 0.35,
      deadCodeInjection: true,
      deadCodeInjectionThreshold: 0.15,
      identifierNamesGenerator: "hexadecimal",
      identifiersPrefix: relative(output, file.path).replace(/[^a-zA-Z0-9]/g, "_"),
      numbersToExpressions: true,
      renameGlobals: false,
      renameProperties: false,
      seed: 1,
      selfDefending: false,
      debugProtection: false,
      sourceMap: false,
      splitStrings: true,
      splitStringsChunkLength: 8,
      stringArray: true,
      stringArrayCallsTransform: true,
      stringArrayCallsTransformThreshold: 0.5,
      stringArrayEncoding: ["base64"],
      stringArrayThreshold: 1,
      target: "browser-no-eval",
    });
    await mkdir(dirname(file.path), { recursive: true });
    await writeFile(file.path, obfuscated.getObfuscatedCode());
  }
}

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
  // Apply the shared theme early, independently of the page bundles.
  await buildJavaScript({
    absWorkingDir: root,
    entryPoints: { common: "src/entries/common.ts" },
    outdir: join(output, "assets"),
    format: "iife",
  });
  // Resolve optional entries on every rebuild so watch sees additions/deletions.
  for (const [format, required, optional] of [
    ["esm", moduleEntries, optionalModuleEntries],
    ["iife", classicEntries, optionalClassicEntries],
  ]) {
    const entryPoints = { ...required };
    for (const [name, path] of Object.entries(optional)) {
      if (existsSync(join(root, path))) entryPoints[name] = path;
    }
    await buildJavaScript({
      absWorkingDir: root,
      entryPoints,
      plugins: [esbuildLang],
      outdir: join(output, "assets"),
      splitting: false,
      format,
    });
  }
  const styles = await build({
    absWorkingDir: root,
    entryPoints: (await readdir(join(source, "styles")))
      .filter((name) => name.endsWith(".css") && name !== "fonts.css")
      .map((name) => join(source, "styles", name)),
    outdir: join(output, "assets"),
    target: "es2020",
    minify: true,
    sourcemap: false,
    legalComments: "none",
    write: false,
    logLevel: "info",
  });
  for (const file of styles.outputFiles) {
    // Python replaces this runtime marker with URI-prefixed font-face rules.
    const marker = file.path === join(output, "assets", "common.css") ? "/* __FONTS__ */" : "";
    await writeFile(file.path, marker + file.text);
  }
  // This is a server-side template, not a browser stylesheet. Keep its URL placeholders intact.
  await cp(join(source, "styles", "fonts.css"), join(output, "assets", "fonts.css"));
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
    watch(join(root, "../lang.jsonc"), schedule),
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

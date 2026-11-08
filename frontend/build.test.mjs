import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { transform } from "esbuild";
import { JSDOM } from "jsdom";

const output = new URL("./dist/", import.meta.url);

test("release assets are compact, valid JavaScript with no source references", async () => {
  const files = await readdir(output, { recursive: true });
  assert.equal(files.some((file) => /\.(?:map|ts|tsx)$/.test(file)), false);
  const bundles = files.filter((file) => file.startsWith("assets/") && file.endsWith(".js"));
  assert.ok(bundles.includes("assets/common.js"));
  assert.ok(bundles.includes("assets/admin/main.js"));
  for (const file of bundles) {
    const code = await readFile(new URL(file, output), "utf8");
    await transform(code, { loader: "js", format: "esm", sourcefile: file });
    assert.equal(code.includes("\n"), false, file);
    assert.doesNotMatch(code, /sourceMappingURL|sourceURL|\/\/\s*src\/|\.tsx?\b/, file);
    assert.match(code, /_0x[\da-f]+/, file);
  }
  for (const file of files.filter((file) => file.startsWith("assets/") && file.endsWith(".css"))) {
    if (file === "assets/fonts.css") continue; // Server-side font URL template.
    const css = await readFile(new URL(file, output), "utf8");
    assert.equal(css.trim().includes("\n"), false, file);
    assert.doesNotMatch(css.replace("/* __FONTS__ */", ""), /\/\*|sourceMappingURL/, file);
    if (file === "assets/common.css") assert.ok(css.startsWith("/* __FONTS__ */"));
  }
});

for (const theme of ["dark", "light"]) {
  test(`obfuscated blocking script applies the ${theme} theme`, async () => {
    const dom = new JSDOM('<!doctype html><button id="themeBtn"></button>', {
      url: "https://example.test/",
      runScripts: "outside-only",
    });
    try {
      dom.window.localStorage.setItem("theme", theme);
      dom.window.eval(await readFile(new URL(join("assets", "common.js"), output), "utf8"));
      assert.equal(dom.window.document.documentElement.dataset.theme, theme);
      assert.ok(dom.window.document.getElementById("themeIconDark"));
      assert.ok(dom.window.document.getElementById("themeIconLight"));
    } finally {
      dom.window.close();
    }
  });
}

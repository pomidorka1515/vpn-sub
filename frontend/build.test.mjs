import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { test } from 'node:test';
import { createContext, Script } from 'node:vm';
import { transform } from 'esbuild';
import { JSDOM } from 'jsdom';

const output = new URL('./dist/', import.meta.url);
const sourceReferences = /sourceMappingURL|sourceURL|\/\/\s*src\/|(?<!\.)\.tsx?\b/;

test('source reference detection does not mistake spread identifiers for paths', () => {
  assert.doesNotMatch('const values = [...ts[key]]', sourceReferences);
  assert.match("import './src/admin.ts'", sourceReferences);
  assert.match("import './src/admin.tsx'", sourceReferences);
});

test('release assets are compact, valid JavaScript with no source references', async () => {
  const files = await readdir(output, { recursive: true });
  assert.equal(
    files.some((file) => /\.(?:map|ts|tsx)$/.test(file)),
    false,
  );
  const bundles = files.filter((file) => file.startsWith('assets/') && file.endsWith('.js'));
  assert.ok(bundles.includes('assets/common.js'));
  assert.ok(bundles.includes('assets/admin/main.js'));
  for (const file of bundles) {
    const code = await readFile(new URL(file, output), 'utf8');
    await transform(code, { loader: 'js', format: 'esm', sourcefile: file });
    assert.equal(code.includes('\n'), false, file);
    assert.doesNotMatch(code, sourceReferences, file);
    assert.match(code, /_0x[\da-f]+/, file);
  }
  for (const file of files.filter((file) => file.startsWith('assets/') && file.endsWith('.css'))) {
    if (file === 'assets/fonts.css') continue; // Server-side font URL template.
    const css = await readFile(new URL(file, output), 'utf8');
    assert.equal(css.trim().includes('\n'), false, file);
    assert.doesNotMatch(css.replace('/* __FONTS__ */', ''), /\/\*|sourceMappingURL/, file);
    if (file === 'assets/common.css') assert.ok(css.startsWith('/* __FONTS__ */'));
  }
});

test('release strings and object keys are not exposed as plaintext or simple Base64', async () => {
  const samples = {
    'common.js': ['data-theme', 'themeIconDark'],
    'auth.js': ['/validate-code', 'current_password', 'toggle_theme'],
    'reset.js': ['/password-reset?token=', 'passwords_not_match'],
    'admin-auth.js': ['invalid_credentials', 'application/json'],
    'dashboard.js': ['/fingerprints', 'current_password', 'bandwidth_unlimited'],
    'history.js': ['chart_load_err', 'current_password'],
    'admin/main.js': ['/api/user/list', '/api/config/set', 'current_password', 'toggle_theme'],
  };
  for (const [file, strings] of Object.entries(samples)) {
    const code = await readFile(new URL(`assets/${file}`, output), 'utf8');
    for (const value of strings) {
      const base64 = Buffer.from(value).toString('base64');
      // The obfuscator's Base64 alphabet swaps the usual upper/lowercase halves.
      const swappedBase64 = base64.replace(/[a-zA-Z]/g, (char) =>
        char === char.toLowerCase() ? char.toUpperCase() : char.toLowerCase(),
      );
      for (const exposed of [value, base64, swappedBase64]) {
        assert.equal(code.includes(exposed), false, `${file}: exposed ${value}`);
      }
    }
  }
});

test('all pages use the soft sage accent instead of the old blue palette', async () => {
  const common = await readFile(new URL('assets/common.css', output), 'utf8');
  assert.match(common, /--accent:\s*#a8c9bc/);
  assert.match(common, /--accent:\s*#285d49/);
  assert.match(common, /color:var\(--accent-ink\)/);
  for (const file of [
    'assets/common.css',
    'assets/admin.css',
    'assets/dashboard.css',
    'templates/404.html',
    'templates/browser.html',
  ]) {
    const content = await readFile(new URL(file, output), 'utf8');
    assert.doesNotMatch(
      content,
      /#(?:6366f1|4f46e5|a5b4fc|0ea5e9|7dd3fc|06b6d4|e0f2fe|075985)\b|rgba\(99,102,241,/i,
      file,
    );
  }
});

test('error pages match their dark colors and use the login entrance animation', async () => {
  const notFound = await readFile(new URL('templates/404.html', output), 'utf8');
  const forbidden = await readFile(new URL('templates/browser.html', output), 'utf8');
  assert.match(notFound, /--box:\s*#131316/);
  assert.match(forbidden, /--surface:\s*#131316/);
  for (const [content, selector] of [
    [notFound, 'main'],
    [forbidden, '\\.box'],
  ]) {
    assert.match(content, /--accent:\s*#a8c9bc/);
    assert.match(content, new RegExp(`${selector} \\{[^}]*animation: fadeUp \\.4s ease`));
    assert.match(content, /@keyframes fadeUp\s*\{\s*from\s*\{\s*opacity:\s*0;\s*transform:\s*translateY\(8px\)/);
    assert.match(
      content,
      new RegExp(`@media \\(prefers-reduced-motion: reduce\\) \\{ ${selector} \\{ animation: none;`),
    );
  }
});

test('pages share theme-aware soft status and usage colors', async () => {
  const css = await readFile(new URL('assets/common.css', output), 'utf8');
  assert.match(css, /--green:\s*#a8c9bc/);
  assert.match(css, /--yellow:\s*#d7c58c/);
  assert.match(css, /--orange:\s*#d9aa80/);
  assert.match(css, /--red:\s*#d88e8e/);
  assert.match(css, /\[data-theme=light\][^{]*\{[^}]*--green:\s*#285d49/);
  assert.match(css, /\[data-theme=light\][^{]*\{[^}]*--red:\s*#982e2e/);
  const dashboard = await readFile(new URL('assets/dashboard.css', output), 'utf8');
  assert.match(dashboard, /--online:\s*#7ecb95/);
  assert.match(dashboard, /\[data-theme=light\][^{]*\{[^}]*--online:\s*#287949/);
  assert.match(dashboard, /\.status-dot\.on\{[^}]*background:var\(--online\)/);
  for (const file of ['common.css', 'admin.css', 'auth.css', 'dashboard.css', 'history.css']) {
    const content = await readFile(new URL(`assets/${file}`, output), 'utf8');
    assert.doesNotMatch(content, /rgba\((?:239,68,68|34,197,94|245,158,11|168,85,247),/, file);
  }
});

for (const theme of ['dark', 'light']) {
  test(`obfuscated blocking script applies the ${theme} theme without runtime code generation`, async () => {
    const dom = new JSDOM('<!doctype html><button id="themeBtn"></button>', {
      url: 'https://example.test/',
      runScripts: 'outside-only',
    });
    try {
      dom.window.localStorage.setItem('theme', theme);
      const context = createContext(
        { document: dom.window.document, localStorage: dom.window.localStorage },
        { codeGeneration: { strings: false, wasm: false } },
      );
      const script = new Script(await readFile(new URL(join('assets', 'common.js'), output), 'utf8'));
      script.runInContext(context, { timeout: 1000 });
      assert.equal(dom.window.document.documentElement.dataset.theme, theme);
      assert.ok(dom.window.document.getElementById('themeIconDark'));
      assert.ok(dom.window.document.getElementById('themeIconLight'));
    } finally {
      dom.window.close();
    }
  });
}

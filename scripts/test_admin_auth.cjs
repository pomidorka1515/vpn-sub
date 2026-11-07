// Run with: node --test scripts/test_admin_auth.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = file => fs.readFileSync(path.join(__dirname, '../frontend/src/legacy', file), 'utf8');

for (const bootstrapped of [true, false]) {
  test(`logout sends session-bound CSRF credential (bootstrapped=${bootstrapped})`, async () => {
    const calls = [];
    const state = { apiToken: bootstrapped ? 'ui.session' : '' };
    const location = { href: '/sub/admin' };
    const context = vm.createContext({
      state, window: { location }, adminPath: suffix => '/sub/admin' + suffix,
      fetch: async (url, options) => {
        calls.push([url, options]);
        return { ok: true, json: async () => ({ obj: { token: 'ui.session' } }) };
      },
      toast() { assert.fail('unexpected logout failure'); }, t: key => key,
    });
    const logout = source('admin/main.js').split('async function doLogout()')[1].split('let leaving')[0];
    vm.runInContext('async function doLogout()' + logout, context);
    await context.doLogout();
    assert.equal(calls.length, bootstrapped ? 1 : 2);
    const [url, options] = calls.at(-1);
    assert.equal(url, '/sub/admin/logout');
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['X-Admin-CSRF'], 'ui.session');
    assert.equal(state.apiToken, '');
    assert.equal(location.href, '/sub/admin/login');
  });
}

test('failed logout does not pretend the session was revoked', async () => {
  const location = { href: '/sub/admin' };
  const toasts = [];
  const context = vm.createContext({
    state: { apiToken: 'ui.session' }, window: { location }, adminPath: suffix => suffix,
    fetch: async () => ({ ok: false, status: 503 }),
    toast: (...args) => toasts.push(args), t: key => key,
  });
  const logout = source('admin/main.js').split('async function doLogout()')[1].split('let leaving')[0];
  vm.runInContext('async function doLogout()' + logout, context);
  await context.doLogout();
  assert.equal(location.href, '/sub/admin');
  assert.deepEqual(toasts, [['network_err', 'error']]);
});

test('browser config accepts an omitted machine token but validates replacements', () => {
  const context = vm.createContext({ t: key => key });
  for (const file of ['config-state.js', 'config-json.js', 'config-validation.js']) {
    const code = source('admin/config/' + file).replace(/^import .*;\n/gm, '').replace(/\bexport /g, '');
    vm.runInContext(code, context);
  }
  const config = fs.readFileSync(path.join(__dirname, '../docs/EXAMPLE.config.json'), 'utf8');
  vm.runInContext(`var draft = parseConfigJson(${JSON.stringify(config)}); delete draft.api_token;`, context);
  assert.equal(vm.runInContext('validateDraft(draft).filter(i => i.path === "api_token").length', context), 0);
  vm.runInContext('draft.api_token = "";', context);
  assert.equal(vm.runInContext('validateDraft(draft).filter(i => i.path === "api_token").length', context), 1);
});

// Run with: node --test scripts/test_dashboard_auth.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function dashboard(message) {
  const elements = new Map();
  const context = vm.createContext({
    API: '/api', AUTH_PAGE: '/login', location: { href: '/dashboard' },
    document: {
      documentElement: { setAttribute() {} },
      getElementById(id) {
        if (id === 'frontend-bootstrap') return { textContent: JSON.stringify({
          prefix: '', language: 'en', translations: {}, fallback: {},
          page: { chartUrl: '/chart.js', debounceMs: 1000 },
        }) };
        if (id === 'themeBtn' || id.startsWith('themeIcon')) return null;
        if (!elements.has(id)) elements.set(id, {
          value: '', textContent: '', disabled: false, isConnected: true,
          setAttribute() {}, removeAttribute() {},
        });
        return elements.get(id);
      },
    },
    setTimeout() {},
    fetch: async () => ({
      status: 401, headers: { get: () => null },
      text: async () => JSON.stringify({ success: false, msg: message }),
    }),
  });
  context.window = context;
  const source = file => fs.readFileSync(path.join(__dirname, '../frontend/src/legacy', file), 'utf8');
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/dist/assets/common.js'), 'utf8'), context);
  context.API = '/api';
  context.AUTH_PAGE = '/login';
  // Load dashboard handlers without starting page initialization/network requests.
  vm.runInContext(source('dashboard.js').split('\napplyTheme();')[0], context);
  context.confirmDialog = async () => 'wrong';
  const toasts = [];
  context.toast = (...args) => toasts.push(args);
  return { context, elements, toasts };
}

for (const action of ['saveSettings', 'doDelete']) {
  for (const message of ['Invalid current password', 'Unauthorized']) {
    test(`${action}: ${message}`, async () => {
      const { context, elements, toasts } = dashboard(message);
      if (action === 'saveSettings') {
        context.document.getElementById('setUser').value = 'newname';
        context.document.getElementById('setCurPass').value = 'wrong';
      }
      await context[action]();
      if (message === 'Unauthorized') {
        assert.equal(context.location.href, '/login');
        assert.deepEqual(toasts, []);
        assert.ok(!elements.get('settingsMsg')?.textContent);
      } else {
        assert.equal(context.location.href, '/dashboard');
        if (action === 'saveSettings') {
          assert.equal(elements.get('settingsMsg').textContent, 'invalid_current_pw');
          assert.equal(elements.get('settingsMsg').className, 'settings-msg err');
        } else {
          assert.deepEqual(toasts, [['invalid_current_pw', 'error']]);
        }
      }
    });
  }
}

test('ordinary requests still redirect on every 401', async () => {
  const { context } = dashboard('Invalid current password');
  assert.equal(await context.api('GET', '/stats'), null);
  assert.equal(context.location.href, '/login');
});

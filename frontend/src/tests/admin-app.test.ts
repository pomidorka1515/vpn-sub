import { required } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initAdmin } from '../admin/main';
import { editor } from '../admin/config/state';

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.(); dispose = undefined;
  editor.fetched = false; editor.loaded = null; editor.draft = null; editor.dirty = false;
});
function setup(request: typeof fetch) {
  const html = readFileSync('src/templates/admin.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script'); bootstrap.type = 'application/json'; bootstrap.id = 'frontend-bootstrap';
  bootstrap.textContent = JSON.stringify({ prefix: '/sub', language: 'en', translations: { codes: 'Codes' }, fallback: { codes: 'Codes' }, languages: { en: { codes: 'Codes' }, ru: { codes: 'Коды' } }, page: { chartUrl: '/chart.js', debounceMs: 20 } });
  document.head.append(bootstrap);
  vi.stubGlobal('fetch', request);
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
  dispose = initAdmin();
}
const token = () => Response.json({ success: true, obj: { token: 'csrf', api_root: '/api', panels_configured: true, permissions: ['*'], catalog: [] } });

describe('admin application integration', () => {
  it('skips unreadable tabs and blocks unauthorized requests before fetching', async () => {
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => Promise.resolve(Response.json({ success: true, obj: { token: 'scoped', api_root: '/api', permissions: ['user.read'], catalog: [] } }))).mockImplementation(() => Promise.resolve(Response.json({ success: true, obj: [] })));
    setup(request);
    await vi.waitFor(() => { expect(required(document.getElementById('tab-users')).classList.contains('active')).toBe(true); });
    await vi.waitFor(() => { expect(request).toHaveBeenCalledTimes(2); });
    const authTab = required(document.querySelector<HTMLButtonElement>('.tab[data-tab="auth"]'));
    expect(authTab.hidden).toBe(false); expect(authTab.disabled).toBe(false); expect(authTab.title).toBeTruthy();
    const health = required(document.querySelector<HTMLButtonElement>('#btnLoadOperations'));
    health.disabled = false; health.click();
    await Promise.resolve(); await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(2);
    expect(request).not.toHaveBeenCalledWith(expect.stringContaining('/api/auth/list'), expect.anything());
  });
  it('opens a restricted tab without issuing requests and returns to an accessible tab', async () => {
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => Promise.resolve(Response.json({ success: true, obj: { token: 'scoped', api_root: '/api', permissions: ['user.read'], catalog: [] } }))).mockImplementation(() => Promise.resolve(Response.json({ success: true, obj: [] })));
    setup(request);
    await vi.waitFor(() => { expect(request).toHaveBeenCalledTimes(2); });
    await vi.waitFor(() => { expect(required(document.getElementById('btnLoadUsers')).hasAttribute('aria-busy')).toBe(false); });
    required(document.querySelector<HTMLButtonElement>('.tab[data-tab="config"]')).click();
    const pane = required(document.getElementById('tab-config'));
    expect(pane.classList.contains('active')).toBe(true);
    expect(pane.classList.contains('permission-restricted')).toBe(true);
    expect(required(pane.querySelector('.permission-tab-notice')).textContent).toContain('permission_missing');
    expect(required(pane.querySelector<HTMLElement>('.config-banner')).inert).toBe(true);
    await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(2);
    required(document.querySelector<HTMLButtonElement>('.tab[data-tab="users"]')).click();
    await vi.waitFor(() => { expect(required(document.getElementById('tab-users')).classList.contains('active')).toBe(true); });
    expect(request).toHaveBeenCalledTimes(3);
  });
  it('shows a restricted tab for an account with no permissions', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ success: true, obj: { token: 'scoped', api_root: '/api', permissions: [], catalog: [] } }));
    setup(request);
    await vi.waitFor(() => { expect(required(document.getElementById('tab-auth')).classList.contains('permission-restricted')).toBe(true); });
    required(document.querySelector<HTMLButtonElement>('.tab[data-tab="health"]')).click();
    expect(required(document.getElementById('tab-health')).classList.contains('active')).toBe(true);
    expect(required(document.querySelector('#tab-health .permission-tab-notice')).textContent).toContain('permission_missing');
    expect(request).toHaveBeenCalledTimes(1);
  });
  it('loads only permitted health sections and never requests online users from its selector', async () => {
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => Promise.resolve(Response.json({ success: true, obj: { token: 'scoped', api_root: '/api', permissions: ['health.read.status'], catalog: [] } }))).mockResolvedValue(Response.json({ success: true, obj: {} }));
    setup(request);
    await vi.waitFor(() => { expect(required(document.getElementById('tab-health')).classList.contains('active')).toBe(true); });
    await vi.waitFor(() => { expect(request).toHaveBeenCalledTimes(2); });
    const online = required(document.querySelector<HTMLInputElement>('#onlineKeyed'));
    expect(online.disabled).toBe(true);
    expect(required(document.getElementById('onlineUsersWrap')).textContent).toContain('permission_missing');
    expect(required(document.getElementById('operationsStatusWrap')).hidden).toBe(true);
    await vi.waitFor(() => { expect(required(document.getElementById('btnLoadOperations')).nextElementSibling?.textContent).toContain('permission_missing'); });
    online.dispatchEvent(new Event('change'));
    await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(2);
  });
  it('shows a denied user-list response instead of leaving the initial loading state', async () => {
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => Promise.resolve(Response.json({ success: true, obj: { token: 'scoped', api_root: '/api', permissions: ['user.read'], catalog: [] } }))).mockResolvedValue(Response.json({ success: false, msg: 'Missing user.read permission' }, { status: 403 }));
    setup(request);
    await vi.waitFor(() => { expect(required(document.getElementById('usersTableWrap')).childElementCount).toBe(0); });
    expect(required(document.getElementById('usersTableWrap')).textContent).toBeTruthy();
    expect(request).toHaveBeenCalledTimes(2);
  });
  it('waits for authentication before refreshing the selected tab and attaches authorization', async () => {
    let resolve!: (response: Response) => void;
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(done => { resolve = done; })).mockImplementation(() => Promise.resolve(Response.json({ success: true, obj: { items: [] } })));
    setup(request);
    required(document.querySelector<HTMLElement>('[data-tab="users"]')).click();
    expect(request).toHaveBeenCalledTimes(1);
    resolve(token());
    await vi.waitFor(() => { expect(request).toHaveBeenCalledWith('https://example.test/api/api/auth/list?kind=accounts', expect.objectContaining({ method: 'GET', headers: { Authorization: 'csrf' } })); });
    expect(required(document.getElementById('bootOverlay')).hidden).toBe(true);
    expect(required(document.getElementById('tab-auth')).classList.contains('active')).toBe(true);
    await vi.waitFor(() => { expect(required(document.getElementById('authaccountsTable')).textContent).toContain('auth_empty'); });
  });
  it('prevents pending authentication from activating a disposed application', async () => {
    let resolve!: (response: Response) => void;
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    setup(request);
    dispose?.(); dispose = undefined;
    resolve(token());
    await Promise.resolve(); await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(1);
    expect(required(document.getElementById('bootOverlay')).hidden).toBe(false);
  });
  it('keeps the selected admin tab and unsaved input when switching languages', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(token());
    setup(request);
    required(document.querySelector<HTMLElement>('.tab[data-tab="codes"]')).click();
    await vi.waitFor(() => { expect(required(document.getElementById('tab-codes')).classList.contains('active')).toBe(true); });
    const field = document.createElement('input');
    field.value = 'unsaved draft';
    required(document.getElementById('tab-codes')).append(field);
    required(document.querySelector<HTMLElement>('.lang-btn[data-lang="ru"]')).click();
    expect(required(document.getElementById('tab-codes')).classList.contains('active')).toBe(true);
    expect(field.value).toBe('unsaved draft');
    expect(field.isConnected).toBe(true);
    expect(document.documentElement.lang).toBe('ru');
    expect(required(document.querySelector<HTMLElement>('.tab[data-tab="codes"]')).textContent).toBe('Коды');
  });
});

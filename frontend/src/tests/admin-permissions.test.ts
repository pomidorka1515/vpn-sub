import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initAuth } from '../admin/auth';
import { createAdminState } from '../admin/bootstrap';
import { allows, applyPermissions, foldPermissions, missingPermission, requestPermissions } from '../admin/permissions';
import type { PermissionEntry } from '../admin/permissions';
import type { AdminContext } from '../admin/context';
import { createLoadGeneration } from '../shared/stale';
import { required } from './required';

const catalog: PermissionEntry[] = [
  { permission: 'auth.read.accounts', requires: null }, { permission: 'auth.create.accounts', requires: 'auth.read.accounts' },
  { permission: 'auth.read.tokens', requires: null }, { permission: 'auth.create.tokens', requires: 'auth.read.tokens' },
  { permission: 'user.read', requires: null }, { permission: 'user.edit.displayname', requires: 'user.read' },
];
let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
function setup(permissions = ['*']) {
  const html = readFileSync('src/templates/admin.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const state = createAdminState(); state.permissions = permissions; state.catalog = catalog;
  const api = vi.fn<AdminContext['api']>().mockResolvedValue({ success: true, status: 200, obj: { items: [] } });
  const context: AdminContext = { doc: document, t: key => key, api, toast: vi.fn(), confirm: vi.fn().mockResolvedValue(true), loadGen: createLoadGeneration(), can: permission => allows(permissions, permission) };
  dispose = initAuth(context, state);
  return { api, state };
}
function click(id: string): void { required(document.getElementById(id)).click(); }
function check(permission: string, checked: boolean): void {
  const target = required(document.querySelector<HTMLInputElement>(`[data-permission="${permission}"]`));
  target.checked = checked; target.dispatchEvent(new Event('change', { bubbles: true }));
}
describe('admin permission model', () => {
  it('matches only explicit leaves or dot-boundary wildcards', () => {
    expect(allows(['user.*'], 'user.edit.time')).toBe(true);
    expect(allows(['user.*'], 'users.read')).toBe(false);
    expect(allows(['user.edit.*'], 'user.read')).toBe(false);
    expect(allows(['user'], 'user.read')).toBe(false);
  });
  it('folds all selections into root and complete branches into wildcards', () => {
    expect(foldPermissions(new Set(catalog.map(entry => entry.permission)), catalog, () => true)).toEqual(['*']);
    expect(foldPermissions(new Set(['user.read', 'user.edit.displayname']), catalog, () => true)).toEqual(['user.*']);
    expect(foldPermissions(new Set(['user.read']), catalog, () => true)).toEqual(['user.read']);
    expect(foldPermissions(new Set(['user.read', 'user.edit.displayname']), catalog, permission => !permission.endsWith('*'))).toEqual(['user.edit.displayname', 'user.read']);
  });
  it('checks every requested user field, dynamic category and config section', () => {
    const state = createAdminState(); state.permissions = ['user.read', 'user.edit.displayname'];
    expect(missingPermission(state, '/api/user/update', { username: 'x', displayname: 'new' })).toBeUndefined();
    expect(missingPermission(state, '/api/user/update', { username: 'x', limit: 1 })).toBe('user.edit.bandwidth.monthly.limit');
    expect(requestPermissions('/api/code/add', { action: 'register' })).toContain('code.add.register');
    expect(requestPermissions('/api/leaderboard', { type: 'wl_monthly' })).toEqual(['leaderboard.read.wl_monthly']);
    expect(requestPermissions('/api/config/set', { values: { '3xui': {}, profiles: {}, api_token: 'new' } })).toEqual(['config.write.panels', 'config.write.inbounds', 'config.write.main', '*']);
    expect(requestPermissions('/api/auth/delete', { kind: 'accounts' })).toEqual(['auth.read.accounts', 'auth.update.accounts']);
    expect(missingPermission(state, '/api/unknown')).toBe('*');
  });
  it('keeps unavailable tabs visible, disabled and explained', () => {
    const { state } = setup(['user.read']); applyPermissions(document, state, key => key);
    const users = required(document.querySelector<HTMLButtonElement>('.tab[data-tab="users"]'));
    const auth = required(document.querySelector<HTMLButtonElement>('.tab[data-tab="auth"]'));
    expect(users.disabled).toBe(false); expect(auth.disabled).toBe(true); expect(auth.hidden).toBe(false);
    expect(auth.title).toBeTruthy(); expect(auth.classList.contains('permission-disabled')).toBe(true);
  });
});
describe('admin identity dialogs', () => {
  it('shows rotation only when editing a key and respects hidden fields in the dialog', async () => {
    const { api } = setup();
    const css = readFileSync('src/styles/admin.css', 'utf8');
    const style = document.createElement('style');
    style.textContent = ['.form-row', '.dark-check', '#authDialog [hidden]'].map(selector => {
      const start = css.indexOf(selector + ' {');
      if (start < 0) throw new Error(`Missing CSS rule: ${selector}`);
      return css.slice(start, css.indexOf('}', start) + 1);
    }).join('\n');
    document.head.append(style);
    try {
      const rotate = required(document.getElementById('authRotateRow'));
      const password = required(document.getElementById('authPasswordRow'));
      click('btnCreateToken');
      expect(getComputedStyle(rotate).display).toBe('none');
      expect(getComputedStyle(password).display).toBe('none');
      click('authClose');

      api.mockImplementation((_method, url) => Promise.resolve({ success: true, status: 200, obj: { items: [{ id: url.endsWith('accounts') ? 'account' : 'key', kind: url.endsWith('accounts') ? 'accounts' : 'tokens', name: 'test', root: false, permissions: [] }] } }));
      click('btnLoadAuth');
      await vi.waitFor(() => { expect(document.querySelectorAll('[data-auth-action="edit"]')).toHaveLength(2); });
      required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="edit"]')).click();
      expect(getComputedStyle(rotate).display).toBe('none');
      expect(getComputedStyle(password).display).not.toBe('none');
      click('authClose');

      required(document.querySelector<HTMLButtonElement>('#authtokensTable [data-auth-action="edit"]')).click();
      expect(getComputedStyle(rotate).display).not.toBe('none');
      expect(getComputedStyle(password).display).toBe('none');
      required(document.querySelector<HTMLInputElement>('#authRotate')).checked = true;
      required(document.getElementById('authForm')).dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      await vi.waitFor(() => { expect(api).toHaveBeenCalledWith('POST', '/api/auth/update', { id: 'key', kind: 'tokens', name: 'test', permissions: [], rotate: true }); });
    } finally { style.remove(); }
  });
  it('opens a create dialog, adds read prerequisites, folds root and shows every tooltip', () => {
    setup(); click('btnCreateAccount');
    expect(required(document.querySelector<HTMLDialogElement>('#authDialog')).open).toBe(true);
    check('user.edit.displayname', true);
    expect(required(document.querySelector<HTMLInputElement>('[data-permission="user.read"]')).checked).toBe(true);
    check('user.read', false);
    expect(required(document.querySelector<HTMLInputElement>('[data-permission="user.edit.displayname"]')).checked).toBe(false);
    check('*', true);
    expect(required(document.getElementById('authPermissionSummary')).textContent).toBe('["*"]');
    expect([...document.querySelectorAll<HTMLLabelElement>('.permission-row')].every(label => !!label.title)).toBe(true);
    expect(required(document.querySelector<HTMLElement>('[data-permission="user.edit.displayname"]')?.parentElement).style.paddingInlineStart).toBe('24px');
  });
  it('does not request unreadable identity kinds or open unauthorized creation', async () => {
    const { api } = setup(['auth.read.tokens']);
    click('btnLoadAuth'); await vi.waitFor(() => { expect(api).toHaveBeenCalledTimes(1); });
    expect(api).toHaveBeenCalledWith('GET', '/api/auth/list?kind=tokens');
    click('btnCreateAccount'); expect(required(document.querySelector<HTMLDialogElement>('#authDialog')).open).toBe(false);
  });
  it('escapes names, reuses three action icons and protects the root row', async () => {
    const { api } = setup();
    api.mockResolvedValue({ success: true, status: 200, obj: { items: [{ id: 'root', kind: 'accounts', name: '<img src=x>', root: true, permissions: ['*'] }] } });
    click('btnLoadAuth'); await vi.waitFor(() => { expect(document.querySelectorAll('#authaccountsTable .user-action')).toHaveLength(3); });
    expect(document.querySelector('#authaccountsTable img')).toBeNull();
    expect(required(document.getElementById('authaccountsTable')).textContent).toContain('<img src=x>');
    expect(document.querySelectorAll('#authaccountsTable .user-action svg')).toHaveLength(3);
    expect(required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="delete"]')).disabled).toBe(true);
    required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="info"]')).click();
    expect(required(document.querySelector<HTMLDialogElement>('#authDialog')).open).toBe(true);
    expect(required(document.querySelector<HTMLInputElement>('#authName')).value).toBe('<img src=x>');
    expect([...document.querySelectorAll<HTMLInputElement>('.permission-row input')].every(field => field.disabled)).toBe(true);
  });
  it('submits normalized permissions and clears a one-time token when closed', async () => {
    const { api } = setup(); click('btnCreateToken'); check('*', true);
    required(document.querySelector<HTMLInputElement>('#authName')).value = 'key';
    api.mockResolvedValueOnce({ success: true, status: 201, obj: { token: 'ak.secret', item: {} } });
    required(document.getElementById('authForm')).dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    await vi.waitFor(() => { expect(api).toHaveBeenCalledWith('POST', '/api/auth/create', { kind: 'tokens', name: 'key', permissions: ['*'] }); });
    await vi.waitFor(() => { expect(required(document.querySelector<HTMLDialogElement>('#authSecretDialog')).open).toBe(true); });
    expect(required(document.querySelector<HTMLInputElement>('#authSecret')).value).toBe('ak.secret');
    click('authSecretClose'); await vi.waitFor(() => { expect(required(document.querySelector<HTMLInputElement>('#authSecret')).value).toBe(''); });
  });
});

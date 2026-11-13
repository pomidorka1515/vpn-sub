import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initAuth } from '../admin/auth';
import { initPermissionReasons } from '../admin/permission-reasons';
import { createAdminState } from '../admin/bootstrap';
import { allows, applyPermissions, foldPermissions, missingPermission, requestPermissions } from '../admin/permissions';
import type { PermissionEntry } from '../admin/permissions';
import type { AdminContext } from '../admin/context';
import { createLoadGeneration } from '../shared/stale';
import { required } from './required';

const catalog: PermissionEntry[] = [
  { permission: 'auth.read.accounts', requires: null },
  { permission: 'auth.create.accounts', requires: 'auth.read.accounts' },
  { permission: 'auth.read.tokens', requires: null },
  { permission: 'auth.create.tokens', requires: 'auth.read.tokens' },
  { permission: 'user.read', requires: null },
  { permission: 'user.edit.displayname', requires: 'user.read' },
];
let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  dispose = undefined;
});
function setup(permissions = ['*'], t: AdminContext['t'] = (key) => key) {
  const html = readFileSync('src/templates/admin.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const state = createAdminState();
  state.permissions = permissions;
  state.catalog = catalog;
  const api = vi.fn<AdminContext['api']>().mockResolvedValue({ success: true, status: 200, obj: { items: [] } });
  const context: AdminContext = {
    doc: document,
    t,
    api,
    toast: vi.fn(),
    confirm: vi.fn().mockResolvedValue(true),
    loadGen: createLoadGeneration(),
    can: (permission) => allows(permissions, permission),
  };
  dispose = initAuth(context, state);
  return { api, state };
}
function click(id: string): void {
  required(document.getElementById(id)).click();
}
function check(permission: string, checked: boolean): void {
  const target = required(document.querySelector<HTMLInputElement>(`[data-permission="${permission}"]`));
  target.checked = checked;
  target.dispatchEvent(new Event('change', { bubbles: true }));
}
describe('admin permission model', () => {
  it('matches only explicit leaves or dot-boundary wildcards', () => {
    expect(allows(['user.*'], 'user.edit.time')).toBe(true);
    expect(allows(['user.*'], 'users.read')).toBe(false);
    expect(allows(['user.edit.*'], 'user.read')).toBe(false);
    expect(allows(['user'], 'user.read')).toBe(false);
  });
  it('folds all selections into root and complete branches into wildcards', () => {
    expect(foldPermissions(new Set(catalog.map((entry) => entry.permission)), catalog, () => true)).toEqual(['*']);
    expect(foldPermissions(new Set(['user.read', 'user.edit.displayname']), catalog, () => true)).toEqual(['user.*']);
    expect(foldPermissions(new Set(['user.read']), catalog, () => true)).toEqual(['user.read']);
    expect(
      foldPermissions(
        new Set(['user.read', 'user.edit.displayname']),
        catalog,
        (permission) => !permission.endsWith('*'),
      ),
    ).toEqual(['user.edit.displayname', 'user.read']);
  });
  it('checks every requested user field, dynamic category and config section', () => {
    const state = createAdminState();
    state.permissions = ['user.read', 'user.edit.displayname'];
    expect(missingPermission(state, '/api/user/update', { username: 'x', displayname: 'new' })).toBeUndefined();
    expect(missingPermission(state, '/api/user/update', { username: 'x', limit: 1 })).toBe(
      'user.edit.bandwidth.monthly.limit',
    );
    expect(requestPermissions('/api/code/add', { action: 'register' })).toContain('code.add.register');
    expect(requestPermissions('/api/leaderboard', { type: 'wl_monthly' })).toEqual(['leaderboard.read.wl_monthly']);
    expect(requestPermissions('/api/config/set', { values: { '3xui': {}, profiles: {}, api_token: 'new' } })).toEqual([
      'config.write.panels',
      'config.write.inbounds',
      'config.write.main',
      '*',
    ]);
    expect(requestPermissions('/api/auth/delete', { kind: 'accounts' })).toEqual([
      'auth.read.accounts',
      'auth.update.accounts',
    ]);
    expect(missingPermission(state, '/api/unknown')).toBe('*');
  });
  it('keeps unavailable tabs navigable and explains their restricted contents', () => {
    const { state } = setup(['user.read']);
    applyPermissions(document, state, (key) => key);
    const users = required(document.querySelector<HTMLButtonElement>('.tab[data-tab="users"]'));
    const auth = required(document.querySelector<HTMLButtonElement>('.tab[data-tab="auth"]'));
    expect(users.disabled).toBe(false);
    expect(auth.disabled).toBe(false);
    expect(auth.hidden).toBe(false);
    expect(auth.title).toBeTruthy();
    expect(auth.classList.contains('permission-disabled')).toBe(true);
    const pane = required(document.getElementById('tab-auth'));
    expect(pane.classList.contains('permission-restricted')).toBe(true);
    expect(required(pane.querySelector('.permission-tab-notice')).textContent).toBe('permission_missing');
    expect(required(pane.querySelector<HTMLElement>('.card')).inert).toBe(true);
    state.permissions = ['*'];
    applyPermissions(document, state, (key) => key);
    expect(pane.classList.contains('permission-restricted')).toBe(false);
    expect(pane.querySelector('.permission-tab-notice')).toBeNull();
    expect(required(pane.querySelector<HTMLElement>('.card')).inert).toBe(false);
  });
  it('blurs inaccessible health cards instead of hiding their contents', () => {
    const { state } = setup(['health.read.status']);
    applyPermissions(document, state, (key) => (key === 'permission_missing' ? 'Missing {permission}' : key));
    const operations = required(document.getElementById('operationsStatusWrap'));
    expect(operations.hidden).toBe(false);
    expect(operations.textContent).toBe('Missing health.read.recovery');
    expect(operations.closest('.card')?.classList.contains('permission-restricted')).toBe(true);
    expect(operations.inert).toBe(true);
    expect(document.querySelectorAll('#tab-health .permission-tab-notice')).toHaveLength(4);
    expect(required(document.getElementById('btnLoadOperations')).title).toBe('Missing health.read.recovery');
    expect(required(document.getElementById('onlineUsersWrap')).textContent).toBe('Missing health.read.online_users');
    expect(required(document.querySelector<HTMLInputElement>('#onlineKeyed')).disabled).toBe(true);
    expect(required(document.getElementById('healthText')).textContent).not.toContain('Missing');
    state.permissions.push('health.read.recovery');
    applyPermissions(document, state, (key) => key);
    expect(operations.hidden).toBe(false);
    expect(operations.inert).toBe(false);
    expect(operations.closest('.card')?.classList.contains('permission-restricted')).toBe(false);
  });
  it.each([
    ['auth.read.accounts', 'auth.read.tokens'],
    ['auth.read.tokens', 'auth.read.accounts'],
    ['state.read.panels', 'state.read.host'],
    ['state.read.host', 'state.read.panels'],
  ])('restricts only the unreadable section with %s', (granted, missing) => {
    const { state } = setup([granted]);
    const t = (key: string) => (key === 'permission_missing' ? 'Missing {permission}' : key);
    applyPermissions(document, state, t);
    const allowed = required(document.querySelector<HTMLElement>(`[data-permission-section="${granted}"]`));
    const denied = required(document.querySelector<HTMLElement>(`[data-permission-section="${missing}"]`));
    expect(allowed.classList.contains('permission-restricted')).toBe(false);
    expect(denied.classList.contains('permission-restricted')).toBe(true);
    const notice = required(denied.querySelector<HTMLElement>(':scope > .permission-tab-notice'));
    expect(notice.textContent).toBe('Missing ' + missing);
    expect(notice.inert).not.toBe(true);
    expect(
      [...denied.children]
        .filter((child) => child !== notice)
        .every((child) => child instanceof HTMLElement && child.inert),
    ).toBe(true);
    applyPermissions(document, state, (key) => (key === 'permission_missing' ? 'Required {permission}' : key));
    expect(notice.textContent).toBe('Required ' + missing);
    expect(denied.querySelectorAll('.permission-tab-notice')).toHaveLength(1);
    state.permissions = ['*'];
    applyPermissions(document, state, t);
    expect(denied.classList.contains('permission-restricted')).toBe(false);
    expect(denied.querySelector('.permission-tab-notice')).toBeNull();
    expect([...denied.children].every((child) => child instanceof HTMLElement && !child.inert)).toBe(true);
  });
  it('marks restricted actions without blurring them', () => {
    const { state } = setup([
      'auth.read.accounts',
      'user.read',
      'code.read',
      'audit.read',
      'snapshots.read',
      'leaderboard.read.total',
      'config.read.main',
    ]);
    applyPermissions(document, state, (key) => key);
    expect(required(document.getElementById('btnCreateAccount')).classList.contains('permission-action-disabled')).toBe(
      true,
    );
    expect(
      required(document.querySelector('.config-tab[data-section="panels"]')).classList.contains(
        'permission-action-disabled',
      ),
    ).toBe(true);
    for (const id of ['btnShowAdd', 'btnShowAddCode', 'btnRefreshLogs', 'btnLoadSnapshots', 'lbType']) {
      expect(required(document.getElementById(id)).classList.contains('permission-action-disabled')).toBe(false);
    }
    for (const tab of ['users', 'codes', 'logs', 'snapshots', 'leaderboard']) {
      expect(document.querySelectorAll(`#tab-${tab} .permission-section`)).toHaveLength(0);
    }
    const css = readFileSync('src/styles/admin.css', 'utf8');
    const rule = css.slice(
      css.indexOf('.permission-action-disabled:not'),
      css.indexOf('}', css.indexOf('.permission-action-disabled:not')),
    );
    expect(rule).not.toContain('blur(');
    expect(rule).toContain('cursor: not-allowed');
  });
  it('summarizes missing actions at the top and updates translations and grants', () => {
    const { state } = setup([
      'auth.read.accounts',
      'auth.read.tokens',
      'user.read',
      'user.edit.displayname',
      'code.read',
      'code.add.bonus',
    ]);
    const translate = (prefix: string) => (key: string) =>
      key === 'permission_missing' ? prefix + ' {permission}' : key;
    applyPermissions(document, state, translate('Missing'));
    const auth = required(document.getElementById('authPermissionNotice'));
    const users = required(document.getElementById('usersPermissionNotice'));
    const codes = required(document.getElementById('codesPermissionNotice'));
    for (const notice of [auth, users, codes]) {
      expect(notice.parentElement?.firstElementChild).toBe(notice);
      expect(notice.hidden).toBe(false);
    }
    expect(auth.textContent).toContain('auth.update.accounts');
    expect(auth.textContent).toContain('auth.create.tokens');
    expect(users.textContent).toContain('user.delete');
    expect(users.textContent).toContain('user.edit.username');
    expect(users.textContent).not.toContain('user.edit.displayname');
    expect(codes.textContent).toContain('code.delete');
    expect(codes.textContent).toContain('code.add.register');
    expect(codes.textContent).not.toContain('code.add.bonus');
    applyPermissions(document, state, translate('Required'));
    expect(users.textContent).toContain('Required');
    state.permissions = ['*'];
    applyPermissions(document, state, translate('Missing'));
    expect([auth, users, codes].every((notice) => notice.hidden && !notice.textContent)).toBe(true);
  });
  it('blurs restricted tabs more strongly than sections while keeping notices sharp', () => {
    const { state } = setup(['health.read.status']);
    const t = (key: string) => (key === 'permission_missing' ? 'Missing {permission}' : key);
    applyPermissions(document, state, t);
    const reasons = initPermissionReasons(document, t);
    const style = document.createElement('style');
    const css = readFileSync('src/styles/admin.css', 'utf8');
    style.textContent = css.slice(css.indexOf('.permission-disabled {'), css.indexOf('.permission-tree {'));
    document.head.append(style);
    try {
      const section = required(document.querySelector('[data-permission-section="health.sync"]'));
      expect(getComputedStyle(required(section.querySelector('.card-title'))).filter).toBe('blur(2px)');
      expect(getComputedStyle(required(section.querySelector('.permission-tab-notice'))).filter).not.toContain('blur');
      expect(section.querySelector('.permission-reason')).toBeNull();
      const tab = required(document.getElementById('tab-logs'));
      expect(getComputedStyle(required(tab.querySelector(':scope > .card'))).filter).toBe('blur(5px)');
      expect(getComputedStyle(required(tab.querySelector(':scope > .permission-tab-notice'))).filter).not.toContain(
        'blur',
      );
    } finally {
      style.remove();
      reasons.dispose();
    }
  });
  it('updates a restricted tab warning when the language changes', () => {
    const { state } = setup(['user.read']);
    applyPermissions(document, state, (key) => (key === 'permission_missing' ? 'Missing {permission}' : key));
    const notice = required(document.querySelector('#tab-logs .permission-tab-notice'));
    expect(notice.textContent).toBe('Missing audit.read');
    applyPermissions(document, state, (key) => (key === 'permission_missing' ? 'Required {permission}' : key));
    expect(notice.textContent).toBe('Required audit.read');
    expect(document.querySelectorAll('#tab-logs .permission-tab-notice')).toHaveLength(1);
  });
  it('explains why online users cannot load when panels are not configured', () => {
    const { state } = setup(['health.read.online_users']);
    state.panelsConfigured = false;
    applyPermissions(document, state, (key) => key);
    expect(required(document.getElementById('onlineUsersWrap')).textContent).toBe('no_panels');
    const checkbox = required(document.querySelector<HTMLInputElement>('#onlineKeyed'));
    expect(checkbox.disabled).toBe(true);
    expect(checkbox.getAttribute('aria-disabled')).toBe('true');
    expect(checkbox.title).toBe('no_panels');
  });
});
describe('admin identity dialogs', () => {
  it.each(['accounts', 'tokens'] as const)(
    'summarizes grant restrictions in the %s create and edit dialogs',
    async (kind) => {
      const grants = [
        'auth.read.accounts',
        'auth.create.accounts',
        'auth.update.accounts',
        'auth.read.tokens',
        'auth.create.tokens',
        'auth.update.tokens',
        'user.read',
      ];
      const t = (key: string) => (key === 'permission_missing' ? 'Missing {permission} permission' : key);
      const { api, state } = setup(grants, t);
      const reasons = initPermissionReasons(document, t);
      try {
        const notice = required(document.getElementById('authGrantNotice'));
        const tree = required(document.getElementById('authPermissionTree'));
        expect(notice.nextElementSibling?.getAttribute('data-t')).toBe('auth_permissions');
        const verify = async () => {
          await Promise.resolve();
          expect(notice.hidden).toBe(false);
          expect(notice.getAttribute('data-t')).toBe('auth_grant_notice');
          expect(required(tree.querySelector<HTMLInputElement>('[data-permission="user.read"]')).disabled).toBe(false);
          expect(
            required(tree.querySelector<HTMLInputElement>('[data-permission="user.edit.displayname"]')).disabled,
          ).toBe(true);
          expect(tree.querySelector('.permission-reason')).toBeNull();
          expect([...tree.querySelectorAll<HTMLInputElement>('input')].every((field) => !field.title)).toBe(true);
        };
        click(kind === 'accounts' ? 'btnCreateAccount' : 'btnCreateToken');
        await verify();
        click('authClose');
        api.mockImplementation((_method, url) => {
          const itemKind = url.endsWith('accounts') ? 'accounts' : 'tokens';
          return Promise.resolve({
            success: true,
            status: 200,
            obj: { items: [{ id: 'item', kind: itemKind, name: 'test', root: false, permissions: [] }] },
          });
        });
        click('btnLoadAuth');
        await vi.waitFor(() => {
          expect(document.querySelector(`#auth${kind}Table [data-auth-action="edit"]`)).not.toBeNull();
        });
        required(document.querySelector<HTMLButtonElement>(`#auth${kind}Table [data-auth-action="edit"]`)).click();
        await verify();
        click('authClose');
        state.permissions = ['*'];
        click(kind === 'accounts' ? 'btnCreateAccount' : 'btnCreateToken');
        expect(notice.hidden).toBe(true);
      } finally {
        reasons.dispose();
      }
    },
  );
  it('shows rotation only when editing a key and respects hidden fields in the dialog', async () => {
    const { api } = setup();
    const css = readFileSync('src/styles/admin.css', 'utf8');
    const style = document.createElement('style');
    style.textContent = ['.form-row', '.dark-check', '#authDialog [hidden]']
      .map((selector) => {
        const start = css.indexOf(selector + ' {');
        if (start < 0) throw new Error(`Missing CSS rule: ${selector}`);
        return css.slice(start, css.indexOf('}', start) + 1);
      })
      .join('\n');
    document.head.append(style);
    try {
      const rotate = required(document.getElementById('authRotateRow'));
      const password = required(document.getElementById('authPasswordRow'));
      click('btnCreateToken');
      expect(getComputedStyle(rotate).display).toBe('none');
      expect(getComputedStyle(password).display).toBe('none');
      click('authClose');

      api.mockImplementation((_method, url) =>
        Promise.resolve({
          success: true,
          status: 200,
          obj: {
            items: [
              {
                id: url.endsWith('accounts') ? 'account' : 'key',
                kind: url.endsWith('accounts') ? 'accounts' : 'tokens',
                name: 'test',
                root: false,
                permissions: [],
              },
            ],
          },
        }),
      );
      click('btnLoadAuth');
      await vi.waitFor(() => {
        expect(document.querySelectorAll('[data-auth-action="edit"]')).toHaveLength(2);
      });
      required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="edit"]')).click();
      expect(getComputedStyle(rotate).display).toBe('none');
      expect(getComputedStyle(password).display).not.toBe('none');
      click('authClose');

      required(document.querySelector<HTMLButtonElement>('#authtokensTable [data-auth-action="edit"]')).click();
      expect(getComputedStyle(rotate).display).not.toBe('none');
      expect(getComputedStyle(password).display).toBe('none');
      required(document.querySelector<HTMLInputElement>('#authRotate')).checked = true;
      required(document.getElementById('authForm')).dispatchEvent(
        new Event('submit', { cancelable: true, bubbles: true }),
      );
      await vi.waitFor(() => {
        expect(api).toHaveBeenCalledWith('POST', '/api/auth/update', {
          id: 'key',
          kind: 'tokens',
          name: 'test',
          permissions: [],
          rotate: true,
        });
      });
    } finally {
      style.remove();
    }
  });
  it('opens a create dialog, adds read prerequisites, folds root and shows every tooltip', () => {
    setup();
    click('btnCreateAccount');
    expect(required(document.querySelector<HTMLDialogElement>('#authDialog')).open).toBe(true);
    check('user.edit.displayname', true);
    expect(required(document.querySelector<HTMLInputElement>('[data-permission="user.read"]')).checked).toBe(true);
    check('user.read', false);
    expect(
      required(document.querySelector<HTMLInputElement>('[data-permission="user.edit.displayname"]')).checked,
    ).toBe(false);
    check('*', true);
    expect(required(document.getElementById('authPermissionSummary')).textContent).toBe('["*"]');
    expect([...document.querySelectorAll<HTMLLabelElement>('.permission-row')].every((label) => !!label.title)).toBe(
      true,
    );
    expect(
      required(document.querySelector<HTMLElement>('[data-permission="user.edit.displayname"]')?.parentElement).style
        .paddingInlineStart,
    ).toBe('24px');
  });
  it('does not request unreadable identity kinds or open unauthorized creation', async () => {
    const { api } = setup(['auth.read.tokens']);
    click('btnLoadAuth');
    await vi.waitFor(() => {
      expect(api).toHaveBeenCalledTimes(1);
    });
    expect(api).toHaveBeenCalledWith('GET', '/api/auth/list?kind=tokens');
    click('btnCreateAccount');
    expect(required(document.querySelector<HTMLDialogElement>('#authDialog')).open).toBe(false);
  });
  it('disables unauthorized identity mutations but keeps details available', async () => {
    const { api } = setup(['auth.read.accounts']);
    api.mockResolvedValue({
      success: true,
      status: 200,
      obj: { items: [{ id: 'account', kind: 'accounts', name: 'test', root: false, permissions: [] }] },
    });
    click('btnLoadAuth');
    await vi.waitFor(() => {
      expect(document.querySelectorAll('#authaccountsTable .user-action')).toHaveLength(3);
    });
    for (const action of ['edit', 'delete']) {
      const target = required(
        document.querySelector<HTMLButtonElement>(`#authaccountsTable [data-auth-action="${action}"]`),
      );
      expect(target.disabled).toBe(true);
      expect(target.classList.contains('permission-action-disabled')).toBe(true);
    }
    expect(
      required(document.querySelector('#authaccountsTable [data-auth-action="info"]')).classList.contains(
        'permission-action-disabled',
      ),
    ).toBe(false);
  });
  it('summarizes missing grants needed to manage an identity', async () => {
    const { api, state } = setup(['auth.read.accounts', 'auth.update.accounts'], (key) =>
      key === 'permission_missing' ? 'Missing {permission}' : key,
    );
    api.mockResolvedValue({
      success: true,
      status: 200,
      obj: { items: [{ id: 'account', kind: 'accounts', name: 'test', root: false, permissions: ['user.read'] }] },
    });
    click('btnLoadAuth');
    await vi.waitFor(() => {
      expect(required(document.getElementById('authPermissionNotice')).textContent).toContain('user.read');
    });
    const edit = required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="edit"]'));
    expect(edit.disabled).toBe(true);
    applyPermissions(document, state, (key) => (key === 'permission_missing' ? 'Required {permission}' : key));
    expect(required(document.getElementById('authPermissionNotice')).textContent).toContain('Required');
    expect(required(document.getElementById('authPermissionNotice')).textContent).toContain('user.read');
  });
  it('replaces the identity loading placeholder on a failed request', async () => {
    const { api } = setup(['auth.read.accounts']);
    api.mockResolvedValue({ success: false, status: 403, msg: 'denied' });
    click('btnLoadAuth');
    await vi.waitFor(() => {
      expect(required(document.getElementById('authaccountsTable')).textContent).toBe('denied');
    });
    expect(required(document.getElementById('authtokensTable')).textContent).toBe('permission_missing');
  });
  it('escapes names, reuses three action icons and protects the root row', async () => {
    const { api } = setup();
    api.mockResolvedValue({
      success: true,
      status: 200,
      obj: { items: [{ id: 'root', kind: 'accounts', name: '<img src=x>', root: true, permissions: ['*'] }] },
    });
    click('btnLoadAuth');
    await vi.waitFor(() => {
      expect(document.querySelectorAll('#authaccountsTable .user-action')).toHaveLength(3);
    });
    expect(document.querySelector('#authaccountsTable img')).toBeNull();
    expect(required(document.getElementById('authaccountsTable')).textContent).toContain('<img src=x>');
    expect(document.querySelectorAll('#authaccountsTable .user-action svg')).toHaveLength(3);
    expect(
      required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="delete"]')).disabled,
    ).toBe(true);
    required(document.querySelector<HTMLButtonElement>('#authaccountsTable [data-auth-action="info"]')).click();
    expect(required(document.querySelector<HTMLDialogElement>('#authDialog')).open).toBe(true);
    expect(required(document.querySelector<HTMLInputElement>('#authName')).value).toBe('<img src=x>');
    expect(
      [...document.querySelectorAll<HTMLInputElement>('.permission-row input')].every((field) => field.disabled),
    ).toBe(true);
    expect(
      [...document.querySelectorAll<HTMLInputElement>('.permission-row input')].every((field) => !field.title),
    ).toBe(true);
    expect(required(document.getElementById('authRootHint')).hidden).toBe(false);
  });
  it('shows one root notice for accounts and keys even when the viewer lacks grants', async () => {
    const { api } = setup(['auth.read.accounts', 'auth.read.tokens']);
    const reasons = initPermissionReasons(document, (key) =>
      key === 'permission_missing' ? 'Missing {permission}' : key,
    );
    try {
      api.mockImplementation((_method, url) => {
        const kind = url.endsWith('accounts') ? 'accounts' : 'tokens';
        return Promise.resolve({
          success: true,
          status: 200,
          obj: { items: [{ id: 'root', kind, name: 'root', root: true, permissions: ['*'] }] },
        });
      });
      click('btnLoadAuth');
      await vi.waitFor(() => {
        expect(document.querySelectorAll('[data-auth-action="info"]')).toHaveLength(2);
      });
      for (const kind of ['accounts', 'tokens']) {
        required(document.querySelector<HTMLButtonElement>(`#auth${kind}Table [data-auth-action="info"]`)).click();
        expect(required(document.getElementById('authRootHint')).hidden).toBe(false);
        expect(
          [...document.querySelectorAll<HTMLInputElement>('#authPermissionTree input')].every(
            (field) => field.disabled && !field.title,
          ),
        ).toBe(true);
        expect(document.querySelector('#authPermissionTree .permission-reason')).toBeNull();
        click('authClose');
      }
      expect(document.querySelector('#tab-auth [data-auth-action="delete"] + .permission-reason')).toBeNull();
    } finally {
      reasons.dispose();
    }
  });
  it.each(['accounts', 'tokens'] as const)(
    'keeps the %s create and edit forms open on a name conflict',
    async (kind) => {
      const { api } = setup();
      const name = required(document.querySelector<HTMLInputElement>('#authName'));
      const dialog = required(document.querySelector<HTMLDialogElement>('#authDialog'));
      const error = required(document.getElementById('authError'));
      const submit = () => {
        required(document.getElementById('authForm')).dispatchEvent(
          new Event('submit', { cancelable: true, bubbles: true }),
        );
      };
      const conflict =
        kind === 'accounts' ? 'An account with this name already exists.' : 'A key with this name already exists.';
      click(kind === 'accounts' ? 'btnCreateAccount' : 'btnCreateToken');
      name.value = 'duplicate';
      if (kind === 'accounts') required(document.querySelector<HTMLInputElement>('#authPassword')).value = 'secret';
      check('user.read', true);
      api.mockResolvedValueOnce({ success: false, status: 409, msg: conflict });
      submit();
      await vi.waitFor(() => {
        expect(error.textContent).toBe(conflict);
      });
      expect(error.hidden).toBe(false);
      expect(dialog.open).toBe(true);
      expect(name.value).toBe('duplicate');
      expect(required(document.querySelector<HTMLInputElement>('[data-permission="user.read"]')).checked).toBe(true);
      if (kind === 'accounts')
        expect(required(document.querySelector<HTMLInputElement>('#authPassword')).value).toBe('secret');
      name.value = 'new';
      name.dispatchEvent(new Event('input', { bubbles: true }));
      expect(error.hidden).toBe(true);
      click('authClose');

      api.mockImplementation((_method, url) =>
        Promise.resolve({
          success: true,
          status: 200,
          obj: {
            items: [
              {
                id: 'item',
                kind: url.endsWith('accounts') ? 'accounts' : 'tokens',
                name: 'original',
                root: false,
                permissions: [],
              },
            ],
          },
        }),
      );
      click('btnLoadAuth');
      await vi.waitFor(() => {
        expect(document.querySelector(`#auth${kind}Table [data-auth-action="edit"]`)).not.toBeNull();
      });
      required(document.querySelector<HTMLButtonElement>(`#auth${kind}Table [data-auth-action="edit"]`)).click();
      name.value = 'duplicate';
      api.mockResolvedValueOnce({ success: false, status: 409, msg: conflict });
      submit();
      await vi.waitFor(() => {
        expect(error.textContent).toBe(conflict);
      });
      expect(dialog.open).toBe(true);
      expect(name.value).toBe('duplicate');
      name.value = 'available';
      name.dispatchEvent(new Event('input', { bubbles: true }));
      api.mockResolvedValueOnce({ success: true, status: 200, obj: { item: {} } });
      submit();
      await vi.waitFor(() => {
        expect(dialog.open).toBe(false);
      });
    },
  );
  it('submits normalized permissions and clears a one-time token when closed', async () => {
    const { api } = setup();
    click('btnCreateToken');
    check('*', true);
    required(document.querySelector<HTMLInputElement>('#authName')).value = 'key';
    api.mockResolvedValueOnce({ success: true, status: 201, obj: { token: 'ak.secret', item: {} } });
    required(document.getElementById('authForm')).dispatchEvent(
      new Event('submit', { cancelable: true, bubbles: true }),
    );
    await vi.waitFor(() => {
      expect(api).toHaveBeenCalledWith('POST', '/api/auth/create', { kind: 'tokens', name: 'key', permissions: ['*'] });
    });
    await vi.waitFor(() => {
      expect(required(document.querySelector<HTMLDialogElement>('#authSecretDialog')).open).toBe(true);
    });
    expect(required(document.querySelector<HTMLInputElement>('#authSecret')).value).toBe('ak.secret');
    click('authSecretClose');
    await vi.waitFor(() => {
      expect(required(document.querySelector<HTMLInputElement>('#authSecret')).value).toBe('');
    });
  });
});

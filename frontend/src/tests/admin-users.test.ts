import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAdminState } from '../admin/bootstrap';
import type { AdminContext } from '../admin/context';
import { initUsers } from '../admin/users';
import { createLoadGeneration } from '../shared/stale';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
function setup() {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  const state = createAdminState();
  const api = vi.fn<AdminContext['api']>();
  const confirm = vi.fn<AdminContext['confirm']>().mockResolvedValue(true);
  const toast = vi.fn();
  dispose = initUsers({ doc: document, t: key => key, api, confirm, toast, loadGen: createLoadGeneration() }, state);
  const click = (id: string) => document.getElementById(id)!.click();
  const input = (id: string) => document.getElementById(id) as HTMLInputElement;
  const load = async (users: unknown = ['alice']) => {
    api.mockResolvedValueOnce({ success: true, obj: users, status: 200 });
    click('btnLoadUsers');
    await vi.waitFor(() => expect(input('btnLoadUsers').disabled).toBe(false));
  };
  return { state, api, confirm, toast, click, input, load };
}
const success = (obj: unknown = null) => ({ success: true, obj, status: 200 });

describe('admin user management', () => {
  it('filters invalid names, escapes markup and disables actions without panels', async () => {
    const { state, load } = setup();
    state.panelsConfigured = false;
    await load(['<img>', null, 42, 'alice']);
    expect(state.currentUsers).toEqual(['<img>', 'alice']);
    const wrap = document.getElementById('usersTableWrap')!;
    expect(wrap.querySelector('img')).toBeNull();
    expect(wrap.textContent).toContain('<img>');
    expect([...wrap.querySelectorAll('button')].every(button => button.disabled)).toBe(true);
  });
  it('ignores outdated details when another user is selected and when details are closed', async () => {
    const { api, load, state, click } = setup();
    await load(['alice', 'bob']);
    let resolveAlice!: (value: ReturnType<typeof success>) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolveAlice = done; })).mockResolvedValueOnce(success({ displayname: 'Bob' }));
    const buttons = document.querySelectorAll<HTMLButtonElement>('[data-user-action="view"]');
    buttons[0]!.click(); buttons[1]!.click();
    await vi.waitFor(() => expect(state.selectedUser).toBe('bob'));
    resolveAlice(success({ displayname: 'Alice' }));
    await Promise.resolve(); await Promise.resolve();
    expect(state.selectedUser).toBe('bob');
    expect(document.getElementById('userDetailInfo')!.textContent).toContain('Bob');
    let resolve!: (value: ReturnType<typeof success>) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    buttons[0]!.click(); click('btnCloseDetail'); resolve(success({ displayname: 'Alice' }));
    await Promise.resolve(); await Promise.resolve();
    expect(state.selectedUser).toBeNull();
    expect(document.getElementById('userDetailCard')!.style.display).toBe('none');
  });
  it('validates required fields and preserves password whitespace in add requests', async () => {
    const { api, click, input, toast } = setup();
    click('btnShowAdd'); click('btnDoAdd');
    expect(toast).toHaveBeenLastCalledWith('internal_user_required', 'error');
    input('addUser').value = ' alice '; click('btnDoAdd');
    expect(toast).toHaveBeenLastCalledWith('display_name_required', 'error');
    input('addDisplayName').value = ' Alice ';
    input('addExtUser').value = ' ext ';
    input('addExtPass').value = ' pass ';
    input('addLimit').value = '10'; input('addWlLimit').value = '0';
    api.mockResolvedValueOnce({ success: false, msg: '<failed>', status: 400 });
    click('btnDoAdd');
    await vi.waitFor(() => expect(document.getElementById('addUserResult')!.textContent).toContain('<failed>'));
    expect(document.getElementById('addUserResult')!.querySelector('failed')).toBeNull();
    expect(api).toHaveBeenCalledWith('POST', '/api/user/add', { user: 'alice', displayname: 'Alice', ext_username: 'ext', ext_password: ' pass ', limit: 10, wl_limit: 0 });
  });
  it('does not delete when confirmation is declined and reloads after success', async () => {
    const { api, confirm, load, state } = setup();
    await load();
    const button = document.querySelector<HTMLButtonElement>('[data-user-action="delete"]')!;
    confirm.mockResolvedValueOnce(false);
    button.click();
    await Promise.resolve();
    expect(api).toHaveBeenCalledTimes(1);
    state.selectedUser = 'alice';
    api.mockResolvedValueOnce(success()).mockResolvedValueOnce(success([]));
    button.click();
    await vi.waitFor(() => expect(state.currentUsers).toEqual([]));
    expect(api).toHaveBeenCalledWith('POST', '/api/user/delete', { user: 'alice' });
    expect(state.selectedUser).toBeNull();
  });
});

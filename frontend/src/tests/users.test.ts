import { readFileSync } from 'node:fs';
import { afterEach, expect, it, vi } from 'vitest';
import { createAdminState } from '../admin/bootstrap';
import type { AdminContext } from '../admin/context';
import { initUsers } from '../admin/users';
import { required, requiredInstance } from './required';

let cleanup: (() => void) | undefined;
afterEach(() => cleanup?.());

function setup(external: string | null = null) {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  vi.spyOn(HTMLDialogElement.prototype, 'showModal').mockImplementation(function (this: HTMLDialogElement) { this.open = true; });
  vi.spyOn(HTMLDialogElement.prototype, 'close').mockImplementation(function (this: HTMLDialogElement) { this.open = false; this.dispatchEvent(new Event('close')); });
  const info = { displayname: 'Alice', ext_username: external, fingerprint: 'chrome', time: 0, enabled: true, bandwidth: { limit: 0, wl_limit: 0 } };
  const api = vi.fn<AdminContext['api']>().mockImplementation((_method, path) => Promise.resolve({ success: true, status: 200, obj: path === '/api/user/list' ? ['alice'] : info }));
  const toast = vi.fn();
  cleanup = initUsers({ doc: document, t: key => key, api, toast, confirm: vi.fn(), loadGen: () => () => true }, createAdminState());
  required(document.getElementById('btnLoadUsers')).click();
  return { api, toast };
}

it('renders three labeled SVG actions and opens unlimited user details in an audit-style dialog', async () => {
  setup();
  await vi.waitFor(() => { expect(document.querySelectorAll('.users-table .user-action')).toHaveLength(3); });
  for (const action of document.querySelectorAll('.users-table .user-action')) {
    expect(action.querySelector('svg')).not.toBeNull();
    expect(action.getAttribute('aria-label')).toBeTruthy();
  }
  required(document.querySelector<HTMLElement>('[data-user-action="view"] svg')).dispatchEvent(new MouseEvent('click', { bubbles: true }));
  const dialog = requiredInstance(document.getElementById('userDetailCard'), HTMLDialogElement);
  await vi.waitFor(() => { expect(dialog.open).toBe(true); });
  expect(dialog.classList.contains('log-dialog')).toBe(true);
  expect(dialog.querySelectorAll('.info-value')).toHaveLength(11);
  expect([...dialog.querySelectorAll('.info-value')].filter(value => value.textContent === '∞')).toHaveLength(3);
  expect(required(document.getElementById('btnEditDetail')).querySelector('svg')).not.toBeNull();
});

it.each([null, 'alice-login'])('edits with external login %s and sends only changed fields', async external => {
  const { api, toast } = setup(external);
  await vi.waitFor(() => { expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull(); });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  const dialog = requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement);
  await vi.waitFor(() => { expect(dialog.open).toBe(true); });
  const login = requiredInstance(document.getElementById('editExtUser'), HTMLInputElement);
  expect(login.disabled).toBe(external === null);
  expect(login.value).toBe(external ?? '');
  requiredInstance(document.getElementById('editDisplayName'), HTMLInputElement).value = 'New Alice';
  if (external) login.value = 'new-login';
  required(document.getElementById('userEditForm')).dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await vi.waitFor(() => { expect(toast).toHaveBeenCalledWith('user_updated', 'success'); });
  expect(api).toHaveBeenCalledWith('POST', '/api/user/update', { username: 'alice', displayname: 'New Alice', ...(external ? { new_username: 'new-login' } : {}) });
  expect(dialog.open).toBe(false);
});

it('caps both expiry fields at the local 2038 timestamp boundary and rejects later dates', async () => {
  const { api } = setup();
  await vi.waitFor(() => { expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull(); });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  await vi.waitFor(() => { expect(requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement).open).toBe(true); });
  const boundary = new Date(2147483647 * 1000);
  const localMax = new Date(boundary.getTime() - boundary.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  for (const id of ['addExpiry', 'editExpiry']) {
    const field = requiredInstance(document.getElementById(id), HTMLInputElement);
    expect(field.max).toBe(localMax);
    field.value = localMax;
    expect(field.checkValidity()).toBe(true);
    field.value = '2038-01-20T00:00';
    expect(field.validity.rangeOverflow).toBe(true);
    field.value = '';
    expect(field.checkValidity()).toBe(true);
    field.value = '2040-01-01T00:00';
  }
  required(document.getElementById('userEditForm')).dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  required(document.getElementById('btnDoAdd')).click();
  expect(api.mock.calls.some(([method]) => method === 'POST')).toBe(false);
});

import { readFileSync } from 'node:fs';
import { afterEach, expect, it, vi } from 'vitest';
import { createAdminState } from '../admin/bootstrap';
import type { AdminContext } from '../admin/context';
import { initUsers } from '../admin/users';
import { required, requiredInstance } from './required';

let cleanup: (() => void) | undefined;
afterEach(() => cleanup?.());

function setup(external: string | null = null, time = 0, timeExact = String(time)) {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  vi.spyOn(HTMLDialogElement.prototype, 'showModal').mockImplementation(function (this: HTMLDialogElement) {
    this.open = true;
  });
  vi.spyOn(HTMLDialogElement.prototype, 'close').mockImplementation(function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  });
  const info = {
    displayname: 'Alice',
    ext_username: external,
    fingerprint: 'chrome',
    time,
    time_exact: timeExact,
    enabled: true,
    bandwidth: { limit: 0, wl_limit: 0 },
  };
  const api = vi.fn<AdminContext['api']>().mockImplementation((_method, path) =>
    Promise.resolve({
      success: true,
      status: 200,
      obj:
        path === '/api/user/list?include_external=1'
          ? ['alice']
          : path === '/api/fingerprints'
            ? ['chrome', 'firefox']
            : info,
    }),
  );
  const toast = vi.fn();
  cleanup = initUsers(
    { doc: document, t: (key) => key, api, toast, confirm: vi.fn(), loadGen: () => () => true },
    createAdminState(),
  );
  required(document.getElementById('btnLoadUsers')).click();
  return { api, toast };
}

it('renders three labeled SVG actions and opens unlimited user details in an audit-style dialog', async () => {
  setup();
  await vi.waitFor(() => {
    expect(document.querySelectorAll('.users-table .user-action')).toHaveLength(3);
  });
  for (const action of document.querySelectorAll('.users-table .user-action')) {
    expect(action.querySelector('svg')).not.toBeNull();
    expect(action.getAttribute('aria-label')).toBeTruthy();
  }
  required(document.querySelector<HTMLElement>('[data-user-action="view"] svg')).dispatchEvent(
    new MouseEvent('click', { bubbles: true }),
  );
  const dialog = requiredInstance(document.getElementById('userDetailCard'), HTMLDialogElement);
  await vi.waitFor(() => {
    expect(dialog.open).toBe(true);
  });
  expect(dialog.classList.contains('log-dialog')).toBe(true);
  expect(dialog.querySelectorAll('.info-value')).toHaveLength(11);
  expect([...dialog.querySelectorAll('.info-value')].filter((value) => value.textContent === '∞')).toHaveLength(3);
  expect(required(document.getElementById('btnEditDetail')).querySelector('svg')).not.toBeNull();
});

it.each([null, 'alice-login'])('edits with external login %s and sends only changed fields', async (external) => {
  const { api, toast } = setup(external);
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull();
  });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  const dialog = requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement);
  await vi.waitFor(() => {
    expect(dialog.open).toBe(true);
  });
  const login = requiredInstance(document.getElementById('editExtUser'), HTMLInputElement);
  expect(login.disabled).toBe(external === null);
  expect(login.value).toBe(external ?? '');
  requiredInstance(document.getElementById('editDisplayName'), HTMLInputElement).value = 'New Alice';
  if (external) login.value = 'new-login';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => {
    expect(toast).toHaveBeenCalledWith('user_updated', 'success');
  });
  expect(api).toHaveBeenCalledWith('POST', '/api/user/update', {
    username: 'alice',
    displayname: 'New Alice',
    ...(external ? { new_username: 'new-login' } : {}),
  });
  expect(dialog.open).toBe(false);
});

it('offers configured fingerprints and submits only a selected change', async () => {
  const { api } = setup();
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull();
  });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  const dialog = requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement);
  await vi.waitFor(() => {
    expect(dialog.open).toBe(true);
  });
  const fp = requiredInstance(document.getElementById('editFp'), HTMLSelectElement);
  expect([...fp.options].map((option) => option.value)).toEqual(['chrome', 'firefox']);
  expect(fp.value).toBe('chrome');
  fp.value = 'invalid';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  expect(api).not.toHaveBeenCalledWith('POST', '/api/user/update', expect.anything());
  fp.value = 'firefox';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => {
    expect(api).toHaveBeenCalledWith('POST', '/api/user/update', { username: 'alice', fingerprint: 'firefox' });
  });
});

it('preserves an unavailable current fingerprint during unrelated edits', async () => {
  const { api } = setup();
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull();
  });
  api.mockResolvedValueOnce({
    success: true,
    status: 200,
    obj: { displayname: 'Alice', fingerprint: 'safari', time: 0, bandwidth: { limit: 0, wl_limit: 0 } },
  });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  await vi.waitFor(() => {
    expect(requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement).open).toBe(true);
  });
  const fp = requiredInstance(document.getElementById('editFp'), HTMLSelectElement);
  expect(fp.value).toBe('safari');
  expect(fp.selectedOptions[0]?.disabled).toBe(true);
  requiredInstance(document.getElementById('editDisplayName'), HTMLInputElement).value = 'New Alice';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => {
    expect(api).toHaveBeenCalledWith('POST', '/api/user/update', { username: 'alice', displayname: 'New Alice' });
  });
});

it('does not open editing when fingerprint choices cannot be loaded', async () => {
  const { api, toast } = setup();
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull();
  });
  api
    .mockResolvedValueOnce({ success: true, status: 200, obj: { fingerprint: 'chrome' } })
    .mockResolvedValueOnce({ success: false, status: 500, msg: 'failed' });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  await vi.waitFor(() => {
    expect(toast).toHaveBeenCalledWith('failed', 'error');
  });
  expect(requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement).open).toBe(false);
});

it('accepts dates beyond 2038 in both expiry fields', async () => {
  const { api } = setup();
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull();
  });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  await vi.waitFor(() => {
    expect(requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement).open).toBe(true);
  });
  for (const id of ['addExpiry', 'editExpiry']) {
    const field = requiredInstance(document.getElementById(id), HTMLInputElement);
    expect(field.max).toBe('9999-12-31T23:59');
    field.value = '2040-01-01T00:00';
    expect(field.checkValidity()).toBe(true);
  }
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => {
    expect(api).toHaveBeenCalledWith('POST', '/api/user/update', {
      username: 'alice',
      time: Math.floor(new Date('2040-01-01T00:00').getTime() / 1000),
    });
  });
});

it('displays exact out-of-range timestamps and preserves them during unrelated edits', async () => {
  const { api } = setup(null, Number('9223372036854775807'), '9223372036854775807');
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="view"]')).not.toBeNull();
  });
  required(document.querySelector<HTMLElement>('[data-user-action="view"]')).click();
  await vi.waitFor(() => {
    expect(required(document.getElementById('userDetailInfo')).textContent).toContain('9223372036854775807');
  });
  required(document.getElementById('btnEditDetail')).click();
  await vi.waitFor(() => {
    expect(requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement).open).toBe(true);
  });
  const expiry = requiredInstance(document.getElementById('editExpiry'), HTMLInputElement);
  expect(expiry.value).toBe('');
  expect(expiry.placeholder).toBe('expiry_out_of_range');
  expect(required(document.getElementById('editExpiryRangeHint')).textContent).toBe('expiry_out_of_range');
  requiredInstance(document.getElementById('editDisplayName'), HTMLInputElement).value = 'New Alice';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => {
    expect(api).toHaveBeenCalledWith('POST', '/api/user/update', { username: 'alice', displayname: 'New Alice' });
  });
});

it('preserves and edits exact usage counters without number rounding', async () => {
  const { api } = setup();
  await vi.waitFor(() => {
    expect(document.querySelector('[data-user-action="edit"]')).not.toBeNull();
  });
  api.mockResolvedValueOnce({
    success: true,
    status: 200,
    obj: {
      displayname: 'Alice',
      fingerprint: 'chrome',
      time: 0,
      bandwidth: { limit: 0, wl_limit: 0, monthly_exact: '9223372036854775807', wl_monthly_exact: '9007199254740993' },
    },
  });
  required(document.querySelector<HTMLElement>('[data-user-action="edit"]')).click();
  await vi.waitFor(() => {
    expect(requiredInstance(document.getElementById('userEditDialog'), HTMLDialogElement).open).toBe(true);
  });
  const used = requiredInstance(document.getElementById('editUsed'), HTMLInputElement);
  const wlUsed = requiredInstance(document.getElementById('editWlUsed'), HTMLInputElement);
  expect(used.value).toBe('9223372036854775807');
  expect(wlUsed.value).toBe('9007199254740993');
  used.value = '9223372036854775808';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  expect(api).not.toHaveBeenCalledWith('POST', '/api/user/update', expect.anything());
  used.value = '0';
  used.dispatchEvent(new Event('input'));
  wlUsed.value = '9007199254740995';
  required(document.getElementById('userEditForm')).dispatchEvent(
    new Event('submit', { bubbles: true, cancelable: true }),
  );
  await vi.waitFor(() => {
    expect(api).toHaveBeenCalledWith('POST', '/api/user/update', {
      username: 'alice',
      used: '0',
      wl_used: '9007199254740995',
    });
  });
});

import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, expect, it, vi } from 'vitest';
import { createAdminState } from '../admin/bootstrap';
import type { AdminContext } from '../admin/context';
import { initHealth } from '../admin/health';
import { initLogs } from '../admin/logs';
import { createSystem } from '../admin/system';
import { initUsers } from '../admin/users';

let cleanup: (() => void) | undefined;
afterEach(() => cleanup?.());

it.each([
  {
    button: 'btnLoadUsers',
    wrap: 'usersTableWrap',
    data: ['alice'],
    init: (ctx: AdminContext) => initUsers(ctx, createAdminState()),
  },
  {
    button: 'btnRefreshLogs',
    wrap: 'logsTableWrap',
    data: [{ ts: 100, action: 'add', info: { user: 'alice' } }],
    init: initLogs,
  },
  {
    button: 'btnLoadSnapshots',
    wrap: 'snapshotsList',
    data: [{ ts: 100, panels: {} }],
    init: (ctx: AdminContext) => createSystem(ctx, () => '', 'en').init(),
  },
  {
    button: 'btnLoadOperations',
    wrap: 'operationsStatusWrap',
    data: {},
    init: (ctx: AdminContext) => initHealth(ctx, 'en'),
  },
  {
    button: 'btnLoadOnline',
    wrap: 'onlineUsersWrap',
    data: { users: ['alice'], panel_health: {} },
    init: (ctx: AdminContext) => initHealth(ctx, 'en'),
  },
  {
    button: 'btnRunHealth',
    wrap: 'healthDetails',
    data: { db: true, uptime: 100, memory: { ram: 20 } },
    init: (ctx: AdminContext) => initHealth(ctx, 'en'),
  },
])('$button preserves content during and after an unchanged refresh', async ({ button, wrap, data, init }) => {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  const api = vi.fn<AdminContext['api']>().mockResolvedValue({ success: true, obj: data, status: 200 });
  cleanup = init({ doc: document, t: (key) => key, api, toast: vi.fn(), confirm: vi.fn(), loadGen: () => () => true });
  const trigger = requiredInstance(document.getElementById(button), HTMLButtonElement);
  const content = required(document.getElementById(wrap));
  trigger.click();
  await vi.waitFor(() => {
    expect(trigger.hasAttribute('aria-busy')).toBe(false);
  });
  const original = content.firstElementChild;
  expect(original).not.toBeNull();
  required(original).classList.add('open');
  const mutations = vi.fn();
  const observer = new MutationObserver(mutations);
  observer.observe(content, { subtree: true, childList: true, attributes: true, characterData: true });
  trigger.click();
  expect(content.firstElementChild).toBe(original);
  await vi.waitFor(() => {
    expect(trigger.hasAttribute('aria-busy')).toBe(false);
  });
  expect(content.firstElementChild).toBe(original);
  expect(mutations).not.toHaveBeenCalled();
  observer.disconnect();
  api.mockResolvedValue({ success: true, obj: null, status: 200 });
  trigger.click();
  await vi.waitFor(() => {
    expect(trigger.hasAttribute('aria-busy')).toBe(false);
  });
  if (button !== 'btnRunHealth' && button !== 'btnLoadOperations') expect(content.firstElementChild).not.toBe(original);
});

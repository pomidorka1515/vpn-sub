import { readFileSync } from 'node:fs';
import { afterEach, expect, it, vi } from 'vitest';
import type { AdminContext } from '../admin/context';
import { initLogs } from '../admin/logs';

let cleanup: (() => void) | undefined;
afterEach(() => cleanup?.());

async function setup() {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  const api = vi.fn<AdminContext['api']>().mockResolvedValue({ success: true, status: 200, obj: [
    { date: '2026-11-09 12:00', action: 'user_add', info: { username: '<alice>', enabled: false, panels: [{ name: 'Main', ports: [443, 8443] }], note: null } },
    { ts: 100, action: 'auth', info: {} },
  ] });
  cleanup = initLogs({ doc: document, t: key => key, api, toast: vi.fn(), confirm: vi.fn(), loadGen: () => () => true });
  const refresh = document.getElementById('btnRefreshLogs') as HTMLButtonElement;
  refresh.click();
  await vi.waitFor(() => expect(refresh.hasAttribute('aria-busy')).toBe(false));
  return { api, refresh, filter: document.getElementById('logActionFilter') as HTMLSelectElement };
}

it('filters loaded actions locally and keeps the selection on refresh', async () => {
  const { api, refresh, filter } = await setup();
  expect([...filter.options].map(option => option.value)).toEqual(['', 'auth', 'user_add']);
  filter.value = 'auth';
  filter.dispatchEvent(new Event('change'));
  expect(document.querySelectorAll('[data-log-index]')).toHaveLength(1);
  expect(document.querySelector('[data-log-index]')?.textContent).toContain('auth');
  expect(api).toHaveBeenCalledTimes(1);
  refresh.click();
  await vi.waitFor(() => expect(refresh.hasAttribute('aria-busy')).toBe(false));
  expect(filter.value).toBe('auth');
  expect(document.querySelectorAll('[data-log-index]')).toHaveLength(1);
  api.mockResolvedValue({ success: true, status: 200, obj: [{ action: 'user_add', info: {} }] });
  refresh.click();
  await vi.waitFor(() => expect(refresh.hasAttribute('aria-busy')).toBe(false));
  expect(filter.value).toBe('');
  expect(document.querySelectorAll('[data-log-index]')).toHaveLength(1);
});

it('opens readable, escaped details with nested fields and keyboard access', async () => {
  await setup();
  const dialog = document.querySelector<HTMLDialogElement>('.log-dialog')!;
  // jsdom does not implement modal dialog behavior.
  dialog.showModal = vi.fn(() => { dialog.setAttribute('open', ''); });
  dialog.close = vi.fn(() => { dialog.removeAttribute('open'); dialog.dispatchEvent(new Event('close')); });
  const row = document.querySelector<HTMLElement>('[data-log-index]')!;
  row.click();
  expect(dialog.showModal).toHaveBeenCalledOnce();
  expect(dialog.textContent).toContain('2026-11-09 12:00');
  expect(dialog.textContent).toContain('user_add');
  expect(dialog.textContent).toContain('<alice>');
  expect(dialog.querySelector('alice')).toBeNull();
  expect(dialog.querySelector('dd .log-fields')).not.toBeNull();
  expect(dialog.querySelectorAll('ol li')).toHaveLength(3);
  expect(dialog.textContent).toContain('no');
  expect(dialog.textContent).toContain('na');
  expect(dialog.textContent).not.toContain('{');
  dialog.querySelector<HTMLButtonElement>('[data-log-close]')!.click();
  expect(dialog.open).toBe(false);
  expect(document.activeElement).toBe(row);
  row.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  expect(dialog.showModal).toHaveBeenCalledTimes(2);
  cleanup?.();
  expect(document.querySelector('.log-dialog')).toBeNull();
});

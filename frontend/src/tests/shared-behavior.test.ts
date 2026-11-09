import { describe, expect, it, vi } from 'vitest';
import { busyDepth, withBusy } from '../shared/busy';
import { createDialogs } from '../shared/dialogs';
import { fmtBytes, fmtTime, fmtUptime } from '../shared/formatting';
import { createNotifications } from '../shared/notifications';
import { createLoadGeneration } from '../shared/stale';
import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';

const t = (key: string) => key;

describe('busy controls', () => {
  it('keeps a nested busy button disabled until the last action settles, including on error', async () => {
    const button = document.createElement('button');
    document.body.append(button);
    let finish!: () => void;
    const outer = withBusy(button, () => new Promise<void>(resolve => { finish = resolve; }));
    expect(busyDepth(button)).toBe(1);
    await expect(withBusy(button, () => { throw new Error('failure'); })).rejects.toThrow('failure');
    expect(busyDepth(button)).toBe(1);
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-busy')).toBe('true');
    finish();
    await outer;
    expect(button.disabled).toBe(false);
    expect(button.hasAttribute('aria-busy')).toBe(false);
    expect(busyDepth(button)).toBe(0);
  });
  it('does not run an independently disabled button and handles missing or detached buttons', async () => {
    const button = document.createElement('button');
    button.disabled = true;
    const action = vi.fn(() => 42);
    expect(await withBusy(button, action)).toBeUndefined();
    expect(action).not.toHaveBeenCalled();
    expect(await withBusy(null, action)).toBe(42);
    button.disabled = false;
    await withBusy(button, action);
    expect(button.disabled).toBe(true);
    expect(busyDepth(button)).toBe(0);
  });
});

describe('dialogs', () => {
  function setup() {
    document.body.innerHTML = '<button id="origin">origin</button><div id="modalOverlay"><div id="modalTitle"></div><div id="modalMsg"></div><input id="modalInput"><button id="modalOk"></button><button id="modalCancel"></button></div>';
    const click = (id: string) => document.getElementById(id)!.click();
    return { ...createDialogs(document, t), click };
  }
  it('submits input with Enter, treats empty input as null, and restores focus', async () => {
    const { confirmDialog, click } = setup();
    click('origin');
    document.getElementById('origin')!.focus();
    const first = confirmDialog('<unsafe>', 'Title', { input: true, inputPlaceholder: 'value' });
    const input = document.getElementById('modalInput') as HTMLInputElement;
    expect(document.getElementById('modalMsg')!.textContent).toBe('<unsafe>');
    expect(document.getElementById('modalMsg')!.children).toHaveLength(0);
    expect(document.activeElement).toBe(input);
    input.value = 'answer';
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(await first).toBe('answer');
    expect(document.activeElement?.id).toBe('origin');
    const second = confirmDialog('', '', { input: true });
    click('modalOk');
    expect(await second).toBeNull();
  });
  it('dismisses replaced dialogs and ignores clicks inside the overlay', async () => {
    const { confirmDialog, dispose, click } = setup();
    const first = confirmDialog('first');
    const second = confirmDialog('second', '', { input: true });
    expect(await first).toBe(false);
    document.getElementById('modalMsg')!.click();
    expect(document.getElementById('modalOverlay')!.classList.contains('show')).toBe(true);
    click('modalOverlay');
    expect(await second).toBeNull();
    const third = confirmDialog('third');
    dispose();
    expect(await third).toBe(false);
  });
});

describe('notifications and state helpers', () => {
  it('replaces and cancels toasts, escapes text, and reuses logout warning', () => {
    const callbacks: (() => void)[] = [];
    const cancel = vi.fn();
    const notifications = createNotifications(document, t, callback => { callbacks.push(callback); return callbacks.length; }, cancel);
    notifications.toast('<b>first</b>');
    expect(document.querySelector('.toast')!.children).toHaveLength(0);
    notifications.toast('second', 'error');
    expect(cancel).toHaveBeenCalledWith(1);
    callbacks[0]!();
    expect(document.querySelector('.toast')!.textContent).toBe('second');
    (document.querySelector('.toast') as HTMLElement).click();
    expect(cancel).toHaveBeenCalledWith(2);
    expect(document.querySelector('.toast')).toBeNull();
    notifications.logoutWarning(); notifications.logoutWarning();
    expect(document.querySelectorAll('#logoutWarning')).toHaveLength(1);
    expect(document.getElementById('logoutWarning')!.textContent).toBe('logout_unconfirmed');
    notifications.dispose();
  });
  it('isolates generation keys and tolerates storage getter and method failures', () => {
    const generation = createLoadGeneration();
    const old = generation('a');
    const other = generation('b');
    const current = generation('a');
    expect([old(), other(), current()]).toEqual([false, true, true]);
    const denied = safeStorage(() => { throw new Error('denied'); });
    expect(denied.getItem('x')).toBeNull();
    expect(() => denied.setItem('x', 'y')).not.toThrow();
    const broken = safeStorage(() => ({ getItem: () => { throw new Error(); }, setItem: () => { throw new Error(); } }) as unknown as Storage);
    expect(broken.getItem('x')).toBeNull();
    expect(() => broken.setItem('x', 'y')).not.toThrow();
  });
  it('persists theme changes and unregisters listeners', () => {
    document.body.innerHTML = '<button id="themeBtn"></button>';
    localStorage.setItem('theme', 'light');
    const theme = createTheme(document, localStorage);
    const listener = vi.fn();
    const unsubscribe = theme.onChange(listener);
    theme.applyTheme();
    expect(document.documentElement.dataset.theme).toBe('light');
    theme.toggleTheme();
    expect(localStorage.getItem('theme')).toBe('dark');
    expect(document.getElementById('themeIconDark')!.style.display).toBe('block');
    unsubscribe(); theme.toggleTheme();
    expect(listener).toHaveBeenCalledTimes(2);
  });
  it('formats boundary and unavailable values', () => {
    expect(fmtBytes(Infinity, t)).toBe('na');
    expect(fmtBytes(-1, t)).toBe('0 byte_unit');
    expect(fmtBytes(1e6, t)).toBe('1.00 mb_unit');
    expect(fmtUptime(null, t)).toBe('na');
    expect(fmtUptime(3661, t)).toBe('1hour_short 1minute_short 1second_short');
    const now = () => 100_000;
    expect(fmtTime(0, t, now)).toBe('unlimited');
    expect(fmtTime(99, t, now)).toBe('expired');
    expect(fmtTime(100, t, now)).toBe('0h_left');
    expect(fmtTime(100 + 86400, t, now)).toBe('1d_left');
  });
});

import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initDashboard } from '../dashboard/page';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
const stats = { displayname: 'Alice', link: 'https://example.test/sub?token=x', uuid: 'uuid', fingerprint: 'chrome', token: 'token', online: true, enabled: true, wl_enabled: true, time: 0,
  bandwidth: { total: { upload: 1000, download: 2000 }, wl_total: { upload: 0, download: 0 }, monthly: 1.5e9, wl_monthly: 1000, limit: 1, wl_limit: 0 } };
const ok = (obj: unknown) => Response.json({ success: true, obj });
function setup() {
  const html = readFileSync('src/templates/dashboard.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script');
  bootstrap.type = 'application/json'; bootstrap.id = 'frontend-bootstrap';
  bootstrap.textContent = JSON.stringify({ prefix: '/sub', language: 'en', translations: { bandwidth_unlimited: '∞' }, fallback: {}, page: { chartUrl: '/chart.js', debounceMs: 20 } });
  document.head.append(bootstrap);
  const request = vi.fn<typeof fetch>().mockImplementation(url => {
    const path = url instanceof Request ? url.url : url.toString();
    if (path.endsWith('/stats')) return Promise.resolve(ok(stats));
    if (path.endsWith('/fingerprints')) return Promise.resolve(ok(['chrome', 'firefox']));
    return Promise.resolve(ok(null));
  });
  vi.stubGlobal('fetch', request);
  const clipboard = { writeText: vi.fn<Clipboard['writeText']>().mockResolvedValue() };
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: clipboard });
  dispose = initDashboard(document, window);
  const click = (id: string) => { required(document.getElementById(id)).click(); };
  const input = (id: string) => requiredInstance(document.getElementById(id), HTMLInputElement);
  const loaded = () => vi.waitFor(() => { expect(input('setName').value).toBe('Alice'); });
  return { request, clipboard, click, input, loaded };
}

describe('dashboard page', () => {
  it('renders status and capped quota bars and copies the localized link', async () => {
    const { click, loaded, clipboard } = setup(); await loaded();
    expect(document.querySelector('.topbar-left')).toBeNull();
    expect(required(document.getElementById('statusText')).textContent).toBe('online');
    expect(required(document.getElementById('statEnabled')).textContent).toBe('activewl_suffix');
    expect(required(document.getElementById('bwMonthlyBar')).style.width).toBe('100%');
    expect(required(document.getElementById('bwMonthlyText')).textContent).toBe('1.50 gb_unit / 1 gb_unit');
    expect(required(document.getElementById('bwWlText')).textContent).toBe('∞');
    expect(required(document.getElementById('bwWlBar')).style.width).toBe('0%');
    click('btnCopy');
    await vi.waitFor(() => { expect(clipboard.writeText).toHaveBeenCalledWith(stats.link + '&lang=en'); });
    expect(required(document.getElementById('btnCopy')).textContent).toBe('copied');
  });
  it('does not overwrite a name being edited on refresh and rejects invalid stats', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('setName').value = 'unsaved'; input('setName').dispatchEvent(new Event('input'));
    const updatedLink = 'https://example.test/sub?token=updated';
    request.mockResolvedValueOnce(ok({ ...stats, displayname: 'Updated', link: updatedLink }));
    click('btnRefresh');
    await vi.waitFor(() => { expect(required(document.getElementById('subLink')).textContent).toBe(updatedLink + '&lang=en'); });
    expect(input('setName').value).toBe('unsaved');
    request.mockResolvedValueOnce(ok({ ...stats, bandwidth: null }));
    click('btnRefresh');
    await vi.waitFor(() => { expect(required(document.querySelector('.toast')).textContent).toBe('invalid_response'); });
    expect(required(document.getElementById('subLink')).textContent).toBe(updatedLink + '&lang=en');
  });
  it('requires the current password for credential changes and sends it without trimming', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('setUser').value = ' newuser ';
    click('btnSave');
    expect(required(document.getElementById('settingsMsg')).textContent).toBe('current_pw_required');
    input('setCurPass').value = ' secret ';
    request.mockResolvedValueOnce(Response.json({ success: false, msg: 'Invalid current password' }, { status: 401 }));
    click('btnSave');
    await vi.waitFor(() => { expect(required(document.getElementById('settingsMsg')).textContent).toBe('invalid_current_pw'); });
    const [path, options] = required(request.mock.calls.at(-1));
    expect(path).toBe('/sub/webapi/settings');
    expect(options?.body).toContain('"current_password":" secret "');
  });
  it('applies a trimmed bonus and refreshes stats only on success', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('bonusInput').value = '  GIFT  ';
    request.mockResolvedValueOnce(ok({ days: 7, gb: 2, perma: true })).mockResolvedValueOnce(ok(stats));
    click('btnApply');
    await vi.waitFor(() => { expect(required(document.getElementById('bonusMsg')).textContent).toBe('+7 days, +2 gb_unit, bonus_reusable'); });
    expect(request).toHaveBeenCalledWith('/sub/webapi/bonus', expect.objectContaining({ body: '{"code":"GIFT"}' }));
    expect(input('bonusInput').value).toBe('');
    await vi.waitFor(() => { expect(request).toHaveBeenCalledTimes(4); });
  });
  it('escapes profile names and descriptions and discards responses after closing', async () => {
    const { request, click, loaded } = setup(); await loaded();
    request.mockResolvedValueOnce(ok({ '<img>': '<script>bad</script>' }));
    click('btnProfiles');
    await vi.waitFor(() => { expect(document.querySelector('.profile-item')).not.toBeNull(); });
    expect(required(document.getElementById('profilesList')).querySelector('script, img')).toBeNull();
    expect(required(document.getElementById('profilesList')).textContent).toContain('<script>bad</script>');
    click('profilesClose');
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    click('btnProfiles'); click('profilesClose');
    resolve(ok({ stale: 'old data' }));
    await Promise.resolve(); await Promise.resolve();
    expect(required(document.getElementById('profilesOverlay')).classList.contains('show')).toBe(false);
    expect(required(document.getElementById('profilesList')).textContent).not.toContain('old data');
  });
  it('requires two confirmations and a password before deletion', async () => {
    const { request, click, loaded } = setup(); await loaded();
    click('btnDelete'); click('modalOk');
    await vi.waitFor(() => { expect(required(document.getElementById('modalTitle')).textContent).toBe('confirm_title_final'); });
    click('modalOk');
    await vi.waitFor(() => { expect((requiredInstance(document.getElementById('modalInput'), HTMLInputElement)).style.display).toBe('block'); });
    (requiredInstance(document.getElementById('modalInput'), HTMLInputElement)).value = 'secret';
    request.mockResolvedValueOnce(Response.json({ success: false, msg: 'Invalid current password' }, { status: 401 }));
    click('modalOk');
    await vi.waitFor(() => { expect(required(document.querySelector('.toast')).textContent).toBe('invalid_current_pw'); });
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/delete', expect.objectContaining({ body: '{"current_password":"secret"}' }));
  });
  it('warns instead of redirecting when logout cannot be confirmed', async () => {
    const { request, click, loaded } = setup(); await loaded();
    request.mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'));
    click('btnLogout');
    await vi.waitFor(() => { expect(required(document.getElementById('logoutWarning')).textContent).toBe('logout_unconfirmed'); });
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/stats', { credentials: 'same-origin', cache: 'no-store' });
  });
  it('renders offline, disabled and expired accounts and both quota limits', async () => {
    const { request, click, loaded } = setup(); await loaded();
    request.mockResolvedValueOnce(ok({ ...stats, displayname: '', online: false, enabled: false, time: 1, bandwidth: { ...stats.bandwidth, limit: 0, wl_limit: 1, wl_monthly: 2e9 } }));
    click('btnRefresh');
    await vi.waitFor(() => { expect(required(document.getElementById('disabledBanner')).textContent).toBe('banner_disabled'); });
    expect(requiredInstance(document.getElementById('setName'), HTMLInputElement).value).toBe('');
    expect(required(document.getElementById('statusText')).textContent).toBe('offline');
    expect(required(document.getElementById('statEnabled')).textContent).toBe('disabled');
    expect(required(document.getElementById('bwMonthlyText')).textContent).toBe('∞');
    expect(required(document.getElementById('bwMonthlyBar')).style.width).toBe('0%');
    expect(required(document.getElementById('bwWlBar')).style.width).toBe('100%');
    expect(required(document.getElementById('bwWlText')).textContent).toBe('2.00 gb_unit / 1 gb_unit');
    request.mockResolvedValueOnce(ok({ ...stats, enabled: true, time: 1 }));
    click('btnRefresh');
    await vi.waitFor(() => { expect(required(document.getElementById('disabledBanner')).textContent).toBe('banner_expired'); });
    expect(required(document.getElementById('disabledBanner')).classList.contains('show')).toBe(true);
  });
  it('handles profile errors, invalid values, empty lists and closing with Escape', async () => {
    const { request, click, loaded } = setup(); await loaded();
    const list = required(document.getElementById('profilesList'));
    request.mockResolvedValueOnce(Response.json({ success: false, msg: '<failure>' }, { status: 500 }))
      .mockResolvedValueOnce(ok({ profile: 123 }))
      .mockResolvedValueOnce(ok({}));
    click('btnProfiles');
    await vi.waitFor(() => { expect(list.textContent).toBe('The request failed. Please retry or contact support.'); });
    expect(list.querySelector('failure')).toBeNull();
    click('btnProfiles');
    await vi.waitFor(() => { expect(list.textContent).toBe('invalid_response'); });
    click('btnProfiles');
    await vi.waitFor(() => { expect(list.textContent).toBe('no_profiles'); });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(required(document.getElementById('profilesOverlay')).classList.contains('show')).toBe(false);
  });
  it('saves changed settings, clears credentials and reloads stats', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('setName').value = ' New Name ';
    input('setName').dispatchEvent(new Event('input'));
    input('setUser').value = ' new-user ';
    input('setPass').value = ' new-password ';
    input('setCurPass').value = ' current ';
    request.mockResolvedValueOnce(ok(null)).mockResolvedValueOnce(ok({ ...stats, displayname: 'New Name' }));
    click('btnSave');
    await vi.waitFor(() => { expect(required(document.getElementById('settingsMsg')).textContent).toBe('saved'); });
    expect(request).toHaveBeenCalledWith('/sub/webapi/settings', expect.objectContaining({ body: JSON.stringify({ name: 'New Name', fingerprint: 'chrome', username: 'new-user', password: ' new-password ', current_password: ' current ' }) }));
    expect(input('setUser').value).toBe('');
    expect(input('setPass').value).toBe('');
    expect(input('setCurPass').value).toBe('');
    await vi.waitFor(() => { expect(input('setName').value).toBe('New Name'); });
  });
  it('does not submit blank bonus codes and preserves a failed code', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('bonusInput').value = '   ';
    click('btnApply');
    expect(request).toHaveBeenCalledTimes(2);
    input('bonusInput').value = ' bad ';
    request.mockResolvedValueOnce(Response.json({ success: false, msg: 'invalid code' }, { status: 400 }));
    input('bonusInput').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await vi.waitFor(() => { expect(required(document.getElementById('bonusMsg')).textContent).toBe('Invalid or exhausted code'); });
    expect(required(document.getElementById('bonusMsg')).className).toBe('bonus-msg err');
    expect(input('bonusInput').value).toBe(' bad ');
    expect(request).toHaveBeenCalledTimes(3);
  });
  it('debounces username validation and ignores a stale result', async () => {
    const { request, input, loaded } = setup(); await loaded();
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise(done => { resolve = done; }))
      .mockResolvedValueOnce(ok({ valid: true, taken: true, sanitized: 'new' }));
    input('setUser').value = 'old'; input('setUser').dispatchEvent(new Event('input'));
    await vi.waitFor(() => { expect(request).toHaveBeenCalledWith('/sub/webapi/settings/validate?username=old', expect.anything()); }, { timeout: 1500 });
    input('setUser').value = 'new'; input('setUser').dispatchEvent(new Event('input'));
    resolve(ok({ valid: false, taken: false, sanitized: 'old' }));
    await vi.waitFor(() => { expect(required(document.getElementById('setUserHint')).textContent).toBe('hint_taken'); }, { timeout: 1500 });
    expect(required(document.getElementById('setUserHint')).className).toBe('username-hint bad');
    input('setUser').value = ''; input('setUser').dispatchEvent(new Event('input'));
    expect(required(document.getElementById('setUserHint')).textContent).toBe('');
  });
});

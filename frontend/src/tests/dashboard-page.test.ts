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
  bootstrap.textContent = JSON.stringify({ prefix: '/sub', language: 'en', translations: {}, fallback: {}, page: { chartUrl: '/chart.js', debounceMs: 20 } });
  document.head.append(bootstrap);
  const request = vi.fn<typeof fetch>().mockImplementation(async url => {
    if (String(url).endsWith('/stats')) return ok(stats);
    if (String(url).endsWith('/fingerprints')) return ok(['chrome', 'firefox']);
    return ok(null);
  });
  vi.stubGlobal('fetch', request);
  const clipboard = { writeText: vi.fn<Clipboard['writeText']>().mockResolvedValue() };
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: clipboard });
  dispose = initDashboard(document, window);
  const click = (id: string) => document.getElementById(id)!.click();
  const input = (id: string) => document.getElementById(id) as HTMLInputElement;
  const loaded = () => vi.waitFor(() => expect(document.getElementById('topName')!.textContent).toBe('Alice'));
  return { request, clipboard, click, input, loaded };
}

describe('dashboard page', () => {
  it('renders status and capped quota bars and copies the localized link', async () => {
    const { click, loaded, clipboard } = setup(); await loaded();
    expect(document.getElementById('statusText')!.textContent).toBe('online');
    expect(document.getElementById('statEnabled')!.textContent).toBe('activewl_suffix');
    expect(document.getElementById('bwMonthlyBar')!.style.width).toBe('100%');
    expect(document.getElementById('bwWlText')!.textContent).toContain('∞');
    click('btnCopy');
    await vi.waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith(stats.link + '&lang=en'));
    expect(document.getElementById('btnCopy')!.textContent).toBe('copied');
  });
  it('does not overwrite a name being edited on refresh and rejects invalid stats', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('setName').value = 'unsaved'; input('setName').dispatchEvent(new Event('input'));
    request.mockResolvedValueOnce(ok({ ...stats, displayname: 'Updated' }));
    click('btnRefresh');
    await vi.waitFor(() => expect(document.getElementById('topName')!.textContent).toBe('Updated'));
    expect(input('setName').value).toBe('unsaved');
    request.mockResolvedValueOnce(ok({ ...stats, bandwidth: null }));
    click('btnRefresh');
    await vi.waitFor(() => expect(document.querySelector('.toast')!.textContent).toBe('invalid_response'));
    expect(document.getElementById('topName')!.textContent).toBe('Updated');
  });
  it('requires the current password for credential changes and sends it without trimming', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('setUser').value = ' newuser ';
    click('btnSave');
    expect(document.getElementById('settingsMsg')!.textContent).toBe('current_pw_required');
    input('setCurPass').value = ' secret ';
    request.mockResolvedValueOnce(Response.json({ success: false, msg: 'Invalid current password' }, { status: 401 }));
    click('btnSave');
    await vi.waitFor(() => expect(document.getElementById('settingsMsg')!.textContent).toBe('invalid_current_pw'));
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/settings', expect.objectContaining({ body: expect.stringContaining('"current_password":" secret "') }));
  });
  it('applies a trimmed bonus and refreshes stats only on success', async () => {
    const { request, click, input, loaded } = setup(); await loaded();
    input('bonusInput').value = '  GIFT  ';
    request.mockResolvedValueOnce(ok({ days: 7, gb: 2, perma: true })).mockResolvedValueOnce(ok(stats));
    click('btnApply');
    await vi.waitFor(() => expect(document.getElementById('bonusMsg')!.textContent).toBe('+7 days, +2 gb_unit, bonus_reusable'));
    expect(request).toHaveBeenCalledWith('/sub/webapi/bonus', expect.objectContaining({ body: '{"code":"GIFT"}' }));
    expect(input('bonusInput').value).toBe('');
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(4));
  });
  it('escapes profile names and descriptions and discards responses after closing', async () => {
    const { request, click, loaded } = setup(); await loaded();
    request.mockResolvedValueOnce(ok({ '<img>': '<script>bad</script>' }));
    click('btnProfiles');
    await vi.waitFor(() => expect(document.querySelector('.profile-item')).not.toBeNull());
    expect(document.getElementById('profilesList')!.querySelector('script, img')).toBeNull();
    expect(document.getElementById('profilesList')!.textContent).toContain('<script>bad</script>');
    click('profilesClose');
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    click('btnProfiles'); click('profilesClose');
    resolve(ok({ stale: 'old data' }));
    await Promise.resolve(); await Promise.resolve();
    expect(document.getElementById('profilesOverlay')!.classList.contains('show')).toBe(false);
    expect(document.getElementById('profilesList')!.textContent).not.toContain('old data');
  });
  it('requires two confirmations and a password before deletion', async () => {
    const { request, click, loaded } = setup(); await loaded();
    click('btnDelete'); click('modalOk');
    await vi.waitFor(() => expect(document.getElementById('modalTitle')!.textContent).toBe('confirm_title_final'));
    click('modalOk');
    await vi.waitFor(() => expect((document.getElementById('modalInput') as HTMLInputElement).style.display).toBe('block'));
    (document.getElementById('modalInput') as HTMLInputElement).value = 'secret';
    request.mockResolvedValueOnce(Response.json({ success: false, msg: 'Invalid current password' }, { status: 401 }));
    click('modalOk');
    await vi.waitFor(() => expect(document.querySelector('.toast')!.textContent).toBe('invalid_current_pw'));
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/delete', expect.objectContaining({ body: '{"current_password":"secret"}' }));
  });
  it('warns instead of redirecting when logout cannot be confirmed', async () => {
    const { request, click, loaded } = setup(); await loaded();
    request.mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'));
    click('btnLogout');
    await vi.waitFor(() => expect(document.getElementById('logoutWarning')!.textContent).toBe('logout_unconfirmed'));
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/stats', { credentials: 'same-origin', cache: 'no-store' });
  });
});

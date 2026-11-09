import { describe, expect, it, vi } from 'vitest';
import { createAdminBootstrap, createAdminState } from '../admin/bootstrap';

function setup() {
  document.body.innerHTML = '<div id="bootOverlay"></div><div class="topbar"><button class="btn-sm" disabled></button><button id="btnLogout"></button></div><div class="admin-content" inert><button id="btnShowAdd"></button><button id="btnDoReset"></button></div>';
  const state = createAdminState();
  const request = vi.fn<typeof fetch>();
  const location = { pathname: '/sub/admin/', origin: 'https://example.test', href: '' };
  const logoutWarning = vi.fn();
  const stopCharts = vi.fn();
  const admin = createAdminBootstrap(state, { doc: document, location, request, translate: key => key, logoutWarning, stopCharts });
  return { state, request, location, logoutWarning, stopCharts, ...admin };
}
const token = (obj: unknown) => Response.json({ success: true, obj });

describe('admin bootstrap', () => {
  it('unlocks the UI only after validating credentials and disables panel actions when unconfigured', async () => {
    const { request, state, bootstrap, config, stopCharts } = setup();
    request.mockResolvedValue(token({ token: 'secret', api_root: '/sub/admin', panels_configured: false }));
    expect(config.guard!()).toBe(true);
    await bootstrap();
    expect(stopCharts).toHaveBeenCalledOnce();
    expect(state).toMatchObject({ apiToken: 'secret', baseUrl: 'https://example.test/sub/admin', panelsConfigured: false });
    expect(config.guard!()).toBe(false);
    expect(document.getElementById('bootOverlay')!.hidden).toBe(true);
    expect((document.getElementById('btnShowAdd') as HTMLButtonElement).disabled).toBe(true);
    expect(document.querySelector('.admin-content')!.textContent).toContain('no_panels');
  });
  it.each([null, {}, { token: '', api_root: '/api' }, { token: 'x', api_root: 4 }])('fails closed on invalid token payload %j', async obj => {
    const { request, state, bootstrap, config } = setup();
    request.mockResolvedValue(token(obj));
    await bootstrap();
    expect(state.authBlocked).toBe(true);
    expect(config.guard!()).toBe(true);
    expect(document.getElementById('bootOverlay')!.textContent).toBe('reload_reauth');
  });
  it('redirects on unauthorized and reports network failure without unlocking', async () => {
    const first = setup();
    first.request.mockResolvedValue(new Response('', { status: 401 }));
    await first.bootstrap();
    expect(first.location.href).toBe('/sub/admin/login');
    const second = setup();
    second.request.mockRejectedValue(new Error('offline'));
    await second.bootstrap();
    expect(second.state.authBlocked).toBe(true);
    expect(document.getElementById('bootOverlay')!.textContent).toBe('network_err');
  });
  it('uses the csrf token and clears credentials only after confirmed logout', async () => {
    const { state, request, logout, location } = setup();
    state.apiToken = 'secret'; state.baseUrl = '/api';
    request.mockResolvedValue(Response.json({ success: true }));
    await logout();
    expect(request).toHaveBeenCalledExactlyOnceWith('/sub/admin/logout', { method: 'POST', credentials: 'same-origin', headers: { 'X-Admin-CSRF': 'secret' } });
    expect(state).toMatchObject({ apiToken: '', baseUrl: '', authBlocked: true });
    expect(location.href).toBe('/sub/admin/login');
  });
  it('keeps the session and warns when logout and the confirmation probe fail', async () => {
    const { state, request, logout, logoutWarning, location } = setup();
    state.apiToken = 'secret'; state.baseUrl = '/api';
    request.mockRejectedValue(new Error('offline'));
    await logout();
    expect(logoutWarning).toHaveBeenCalledOnce();
    expect(state).toMatchObject({ apiToken: 'secret', baseUrl: '/api', authBlocked: false });
    expect(location.href).toBe('');
    expect(request).toHaveBeenLastCalledWith('/sub/admin/token', { credentials: 'same-origin', cache: 'no-store' });
  });
  it('accepts a 401 probe as proof of logout and fetches a missing csrf token', async () => {
    const { request, logout, location } = setup();
    request.mockResolvedValueOnce(token({ token: 'fresh' })).mockRejectedValueOnce(new Error('lost response')).mockResolvedValueOnce(new Response('', { status: 401 }));
    await logout();
    expect(request).toHaveBeenNthCalledWith(2, '/sub/admin/logout', expect.objectContaining({ headers: { 'X-Admin-CSRF': 'fresh' } }));
    expect(location.href).toBe('/sub/admin/login');
  });
  it('ignores an in-flight bootstrap after disposal', async () => {
    const { request, bootstrap, dispose, state } = setup();
    let resolve!: (response: Response) => void;
    request.mockImplementation(() => new Promise(done => { resolve = done; }));
    const pending = bootstrap();
    dispose();
    resolve(token({ token: 'secret', api_root: '/api' }));
    await pending;
    expect(state.apiToken).toBe('');
    expect(document.getElementById('bootOverlay')!.hidden).toBe(false);
  });
});

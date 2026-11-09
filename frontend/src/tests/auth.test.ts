import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initAdminAuth } from '../auth/admin';
import { initAuth } from '../auth/user';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });

function setup(admin = false) {
  const html = readFileSync(`src/templates/${admin ? 'admin-auth' : 'auth'}.html`, 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script');
  bootstrap.id = 'frontend-bootstrap';
  bootstrap.type = 'application/json';
  bootstrap.textContent = JSON.stringify({ prefix: '/sub', language: 'en', translations: {}, fallback: {}, page: { chartUrl: '/chart.js', debounceMs: 10 } });
  document.head.append(bootstrap);
  const navigate = vi.fn();
  const request = vi.fn<typeof fetch>().mockResolvedValue(new Response('', { status: 401 }));
  dispose = admin ? initAdminAuth(document, request, navigate) : initAuth(document, request, navigate);
  const field = (id: string) => document.getElementById(id) as HTMLInputElement;
  const click = (id: string) => document.getElementById(id)!.click();
  return { request, navigate, field, click };
}

describe('user authentication', () => {
  it('redirects an existing session and suppresses redirects after disposal', async () => {
    const { request, navigate } = setup();
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith('/sub/webapi/stats', { credentials: 'same-origin' }));
    expect(navigate).not.toHaveBeenCalled();
    dispose?.();
    const request2 = vi.fn<typeof fetch>();
    let resolve!: (response: Response) => void;
    request2.mockImplementation(() => new Promise(done => { resolve = done; }));
    dispose = initAuth(document, request2, navigate);
    dispose();
    resolve(new Response('', { status: 200 }));
    await Promise.resolve();
    expect(navigate).not.toHaveBeenCalled();
  });
  it('submits credentials once while pending and navigates on success', async () => {
    const { request, navigate, field, click } = setup();
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    field('loginUser').value = 'alice'; field('loginPass').value = 'secret';
    click('loginBtn'); click('loginBtn');
    expect(request).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/login', expect.objectContaining({ method: 'POST', body: JSON.stringify({ username: 'alice', password: 'secret' }) }));
    resolve(Response.json({ success: true }));
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith('/sub/panel'));
    expect((document.getElementById('loginBtn') as HTMLButtonElement).disabled).toBe(false);
  });
  it('validates password confirmation and falls back to login if automatic login fails', async () => {
    const { request, field, click, navigate } = setup();
    click('tabRegister');
    field('regUser').value = 'alice'; field('regPass').value = 'secret'; field('regPass2').value = 'different';
    click('regBtn');
    expect(document.getElementById('authError')!.textContent).toBe('passwords_not_match');
    expect(request).toHaveBeenCalledTimes(1);
    field('regPass2').value = 'secret'; field('regCode').value = 'invite'; field('regName').value = 'Alice';
    request.mockResolvedValueOnce(Response.json({ success: true })).mockRejectedValueOnce(new Error('offline'));
    click('regBtn');
    await vi.waitFor(() => expect(document.getElementById('authError')!.textContent).toBe('account_created_login_failednetwork_err'));
    expect(field('loginUser').value).toBe('alice');
    expect(document.getElementById('loginForm')!.style.display).toBe('block');
    expect(navigate).not.toHaveBeenCalled();
    expect(request).toHaveBeenNthCalledWith(2, '/sub/webapi/register', expect.objectContaining({ body: JSON.stringify({ username: 'alice', password: 'secret', code: 'invite', name: 'Alice' }) }));
  });
  it('ignores an outdated username validation response', async () => {
    const { request, field } = setup();
    vi.useFakeTimers();
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    field('regUser').value = 'old&name'; field('regUser').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/validate?username=old%26name', expect.anything());
    field('regUser').value = '';
    field('regUser').dispatchEvent(new Event('input'));
    resolve(Response.json({ success: true, obj: { valid: true, taken: true } }));
    await vi.advanceTimersByTimeAsync(0);
    expect(document.getElementById('usernameHint')!.textContent).toBe('');
  });
});

describe('admin authentication', () => {
  it('requires both HTTP success and a valid success envelope', async () => {
    const { request, navigate, field, click } = setup(true);
    field('loginUser').value = 'admin'; field('loginPass').value = 'secret';
    request.mockResolvedValueOnce(new Response('not json', { status: 200 }));
    click('loginBtn');
    await vi.waitFor(() => expect(document.getElementById('authError')!.textContent).toBe('invalid_credentials'));
    expect(navigate).not.toHaveBeenCalled();
    request.mockResolvedValueOnce(Response.json({ success: true }));
    click('loginBtn');
    await vi.waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith('/sub/admin'));
    expect(request).toHaveBeenLastCalledWith('/sub/admin/session', expect.objectContaining({ method: 'POST', body: JSON.stringify({ username: 'admin', password: 'secret' }) }));
  });
  it('does not redirect on a successful response after disposal', async () => {
    const { request, navigate, click } = setup(true);
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    click('loginBtn');
    dispose?.();
    resolve(Response.json({ success: true }));
    await vi.waitFor(() => expect((document.getElementById('loginBtn') as HTMLButtonElement).disabled).toBe(false));
    expect(navigate).not.toHaveBeenCalled();
  });
});

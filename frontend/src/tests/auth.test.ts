import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initAdminAuth } from '../auth/admin';
import { initAuth } from '../auth/user';

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  dispose = undefined;
});

function setup(admin = false) {
  const html = readFileSync(`src/templates/${admin ? 'admin-auth' : 'auth'}.html`, 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script');
  bootstrap.id = 'frontend-bootstrap';
  bootstrap.type = 'application/json';
  bootstrap.textContent = JSON.stringify({
    prefix: '/sub',
    language: 'en',
    translations: {},
    fallback: {},
    languages: { en: { create_account: 'Create account' }, ru: { create_account: 'Создать аккаунт' } },
    page: { chartUrl: '/chart.js', debounceMs: 10 },
  });
  document.head.append(bootstrap);
  const navigate = vi.fn();
  const request = vi.fn<typeof fetch>().mockResolvedValue(new Response('', { status: 401 }));
  dispose = admin ? initAdminAuth(document, request, navigate) : initAuth(document, request, navigate);
  const field = (id: string) => requiredInstance(document.getElementById(id), HTMLInputElement);
  const click = (id: string) => {
    required(document.getElementById(id)).click();
  };
  return { request, navigate, field, click };
}

describe('user authentication', () => {
  it('changes language without leaving the registration tab or losing input', () => {
    const { click, field } = setup();
    click('tabRegister');
    field('regUser').value = 'alice';
    required(document.querySelector<HTMLElement>('.lang-btn[data-lang="ru"]')).click();
    expect(field('regUser').value).toBe('alice');
    expect(required(document.getElementById('registerForm')).style.display).toBe('block');
    expect(required(document.getElementById('authTitle')).textContent).toBe('Создать аккаунт');
    expect(document.title).toBe('Создать аккаунт');
  });
  it('redirects an existing session and suppresses redirects after disposal', async () => {
    const { request, navigate } = setup();
    await vi.waitFor(() => {
      expect(request).toHaveBeenCalledWith('/sub/webapi/stats', { credentials: 'same-origin' });
    });
    expect(navigate).not.toHaveBeenCalled();
    dispose?.();
    const request2 = vi.fn<typeof fetch>();
    let resolve!: (response: Response) => void;
    request2.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    dispose = initAuth(document, request2, navigate);
    dispose();
    resolve(new Response('', { status: 200 }));
    await Promise.resolve();
    expect(navigate).not.toHaveBeenCalled();
  });
  it('submits credentials once while pending and navigates on success', async () => {
    const { request, navigate, field, click } = setup();
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    field('loginUser').value = 'alice';
    field('loginPass').value = 'secret';
    click('loginBtn');
    click('loginBtn');
    expect(request).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenLastCalledWith(
      '/sub/webapi/login',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ username: 'alice', password: 'secret' }) }),
    );
    resolve(Response.json({ success: true }));
    await vi.waitFor(() => {
      expect(navigate).toHaveBeenCalledExactlyOnceWith('/sub/panel');
    });
    expect(requiredInstance(document.getElementById('loginBtn'), HTMLButtonElement).disabled).toBe(false);
  });
  it('validates password confirmation and falls back to login if automatic login fails', async () => {
    const { request, field, click, navigate } = setup();
    click('tabRegister');
    vi.useFakeTimers();
    field('regCode').value = 'invite';
    request.mockResolvedValueOnce(Response.json({ success: true, obj: { valid: true } }));
    field('regCode').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    vi.useRealTimers();
    field('regUser').value = 'alice';
    field('regPass').value = 'secret';
    field('regPass2').value = 'different';
    click('regBtn');
    expect(required(document.getElementById('authError')).textContent).toBe('passwords_not_match');
    expect(request).toHaveBeenCalledTimes(2);
    field('regPass2').value = 'secret';
    field('regCode').value = 'invite';
    field('regName').value = 'Alice';
    request.mockResolvedValueOnce(Response.json({ success: true })).mockRejectedValueOnce(new Error('offline'));
    click('regBtn');
    await vi.waitFor(() => {
      expect(required(document.getElementById('authError')).textContent).toBe(
        'account_created_login_failednetwork_err',
      );
    });
    expect(field('loginUser').value).toBe('alice');
    expect(required(document.getElementById('loginForm')).style.display).toBe('block');
    expect(navigate).not.toHaveBeenCalled();
    expect(request).toHaveBeenNthCalledWith(
      3,
      '/sub/webapi/register',
      expect.objectContaining({
        body: JSON.stringify({ username: 'alice', password: 'secret', code: 'invite', name: 'Alice' }),
      }),
    );
  });
  it('ignores an outdated username validation response', async () => {
    const { request, field } = setup();
    vi.useFakeTimers();
    request.mockResolvedValueOnce(Response.json({ success: true, obj: { valid: true } }));
    field('regCode').value = 'invite';
    field('regCode').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    field('regUser').value = 'old&name';
    field('regUser').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    expect(request).toHaveBeenLastCalledWith('/sub/webapi/validate?username=old%26name&code=invite', expect.anything());
    field('regUser').value = '';
    field('regUser').dispatchEvent(new Event('input'));
    resolve(Response.json({ success: true, obj: { valid: true, taken: true } }));
    await vi.advanceTimersByTimeAsync(0);
    expect(required(document.getElementById('usernameHint')).textContent).toBe('');
  });
  it('requires a verified code before validating usernames or registering', async () => {
    const { request, field, click } = setup();
    vi.useFakeTimers();
    const register = requiredInstance(document.getElementById('regBtn'), HTMLButtonElement);
    expect(register.disabled).toBe(true);
    field('regUser').value = 'alice';
    field('regUser').dispatchEvent(new Event('input'));
    field('regPass').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    click('regBtn');
    await vi.advanceTimersByTimeAsync(400);
    expect(request).toHaveBeenCalledTimes(1);
    request.mockResolvedValueOnce(Response.json({ success: true, obj: { valid: false } }));
    field('regCode').value = 'bonus';
    field('regCode').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    expect(register.disabled).toBe(true);
    expect(required(document.getElementById('codeHint')).textContent).toBe('code_invalid');
    expect(request).toHaveBeenCalledTimes(2);
    request
      .mockResolvedValueOnce(Response.json({ success: true, obj: { valid: true } }))
      .mockResolvedValueOnce(Response.json({ success: true, obj: { valid: true, taken: false } }));
    field('regCode').value = 'invite&code';
    field('regCode').dispatchEvent(new Event('input'));
    expect(register.disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(400);
    expect(request).toHaveBeenLastCalledWith(
      '/sub/webapi/validate-code',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ code: 'invite&code' }) }),
    );
    expect(register.disabled).toBe(false);
    expect(required(document.getElementById('codeHint')).textContent).toBe('code_valid');
    await vi.advanceTimersByTimeAsync(400);
    expect(request).toHaveBeenLastCalledWith(
      '/sub/webapi/validate?username=alice&code=invite%26code',
      expect.anything(),
    );
    expect(required(document.getElementById('usernameHint')).textContent).toBe('hint_ok');
    field('regCode').value = '';
    field('regCode').dispatchEvent(new Event('input'));
    expect(register.disabled).toBe(true);
    expect(required(document.getElementById('usernameHint')).textContent).toBe('');
    expect(required(document.getElementById('codeHint')).textContent).toBe('code_required');
  });
  it('ignores stale code and username results after the code changes', async () => {
    const { request, field } = setup();
    vi.useFakeTimers();
    let resolveCode!: (response: Response) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolveCode = done;
        }),
    );
    field('regCode').value = 'old';
    field('regCode').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    field('regCode').value = 'new';
    field('regCode').dispatchEvent(new Event('input'));
    resolveCode(Response.json({ success: true, obj: { valid: true } }));
    await vi.advanceTimersByTimeAsync(0);
    expect(requiredInstance(document.getElementById('regBtn'), HTMLButtonElement).disabled).toBe(true);
    request.mockResolvedValueOnce(Response.json({ success: true, obj: { valid: true } }));
    await vi.advanceTimersByTimeAsync(400);
    let resolveUsername!: (response: Response) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolveUsername = done;
        }),
    );
    field('regUser').value = 'alice';
    field('regUser').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    field('regCode').value = '';
    field('regCode').dispatchEvent(new Event('input'));
    resolveUsername(Response.json({ success: true, obj: { valid: true, taken: true } }));
    await vi.advanceTimersByTimeAsync(0);
    expect(required(document.getElementById('usernameHint')).textContent).toBe('');
    expect(requiredInstance(document.getElementById('regBtn'), HTMLButtonElement).disabled).toBe(true);
  });
  it.each([
    () => Promise.reject(new Error('offline')),
    () => Promise.resolve(Response.json({ success: true, obj: { valid: true } }, { status: 503 })),
    () => Promise.resolve(Response.json({ success: true, obj: {} })),
  ])('fails closed when the code check fails', async (response) => {
    const { request, field } = setup();
    vi.useFakeTimers();
    request.mockImplementationOnce(response);
    field('regCode').value = 'invite';
    field('regCode').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    expect(requiredInstance(document.getElementById('regBtn'), HTMLButtonElement).disabled).toBe(true);
    expect(required(document.getElementById('codeHint')).textContent).toBe('code_check_failed');
  });
  it('discards pending code checks on disposal', async () => {
    const { request, field } = setup();
    vi.useFakeTimers();
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    field('regCode').value = 'invite';
    field('regCode').dispatchEvent(new Event('input'));
    await vi.advanceTimersByTimeAsync(400);
    dispose?.();
    resolve(Response.json({ success: true, obj: { valid: true } }));
    await vi.advanceTimersByTimeAsync(400);
    expect(requiredInstance(document.getElementById('regBtn'), HTMLButtonElement).disabled).toBe(true);
    expect(request).toHaveBeenCalledTimes(2);
  });
});

describe('admin authentication', () => {
  it('requires both HTTP success and a valid success envelope', async () => {
    const { request, navigate, field, click } = setup(true);
    field('loginUser').value = 'admin';
    field('loginPass').value = 'secret';
    request.mockResolvedValueOnce(new Response('not json', { status: 200 }));
    click('loginBtn');
    await vi.waitFor(() => {
      expect(required(document.getElementById('authError')).textContent).toBe('invalid_credentials');
    });
    expect(navigate).not.toHaveBeenCalled();
    request.mockResolvedValueOnce(Response.json({ success: true }));
    click('loginBtn');
    await vi.waitFor(() => {
      expect(navigate).toHaveBeenCalledExactlyOnceWith('/sub/admin');
    });
    expect(request).toHaveBeenLastCalledWith(
      '/sub/admin/session',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ username: 'admin', password: 'secret' }) }),
    );
  });
  it('does not redirect on a successful response after disposal', async () => {
    const { request, navigate, click } = setup(true);
    let resolve!: (response: Response) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    click('loginBtn');
    dispose?.();
    resolve(Response.json({ success: true }));
    await vi.waitFor(() => {
      expect(requiredInstance(document.getElementById('loginBtn'), HTMLButtonElement).disabled).toBe(false);
    });
    expect(navigate).not.toHaveBeenCalled();
  });
});

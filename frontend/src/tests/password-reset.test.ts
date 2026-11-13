import { readFileSync } from 'node:fs';
import { afterEach, expect, it, vi } from 'vitest';
import { initPasswordReset } from '../auth/reset';
import { required, requiredInstance } from './required';

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  dispose = undefined;
  window.history.replaceState(null, '', '/');
});

function setup() {
  window.history.replaceState(null, '', '/nested/sub/reset?token=reset-token');
  const html = readFileSync('src/templates/reset.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script');
  bootstrap.id = 'frontend-bootstrap';
  bootstrap.type = 'application/json';
  bootstrap.textContent = JSON.stringify({
    prefix: '/nested/sub',
    language: 'en',
    translations: {},
    fallback: {},
    languages: { en: {}, ru: {} },
    page: { chartUrl: '/chart.js', debounceMs: 10 },
  });
  document.head.append(bootstrap);
  const request = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ success: true }));
  dispose = initPasswordReset(document, request);
  const password = requiredInstance(document.getElementById('resetPass'), HTMLInputElement);
  const confirmation = requiredInstance(document.getElementById('resetPass2'), HTMLInputElement);
  const submit = () => {
    required(document.getElementById('resetForm')).dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    );
  };
  return { request, password, confirmation, submit };
}

it('validates confirmation and resets without a login or username', async () => {
  const { request, password, confirmation, submit } = setup();
  password.value = ' new-password ';
  confirmation.value = 'different';
  submit();
  expect(request).not.toHaveBeenCalled();
  expect(required(document.getElementById('authError')).textContent).toBe('passwords_not_match');
  confirmation.value = password.value;
  submit();
  submit();
  expect(request).toHaveBeenCalledExactlyOnceWith(
    '/nested/sub/webapi/password-reset?token=reset-token',
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ password: ' new-password ' }) }),
  );
  await vi.waitFor(() => {
    expect(required(document.getElementById('resetSuccess')).hidden).toBe(false);
  });
  expect(required(document.getElementById('resetForm')).hidden).toBe(true);
  expect(password.value).toBe('');
  expect(confirmation.value).toBe('');
  expect(window.location.search).toBe('');
});

it('shows server errors and lets the user retry', async () => {
  const { request, password, confirmation, submit } = setup();
  password.value = confirmation.value = 'new-password';
  request.mockResolvedValueOnce(Response.json({ success: false, msg: 'Not found' }, { status: 404 }));
  submit();
  await vi.waitFor(() => {
    expect(requiredInstance(document.getElementById('resetBtn'), HTMLButtonElement).disabled).toBe(false);
  });
  expect(required(document.getElementById('authError')).classList.contains('show')).toBe(true);
  expect(required(document.getElementById('resetSuccess')).hidden).toBe(true);
  submit();
  await vi.waitFor(() => {
    expect(required(document.getElementById('resetSuccess')).hidden).toBe(false);
  });
});

it('ignores responses after disposal', async () => {
  const { request, password, confirmation, submit } = setup();
  let resolve!: (response: Response) => void;
  request.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  password.value = confirmation.value = 'new-password';
  submit();
  dispose?.();
  resolve(Response.json({ success: true }));
  await Promise.resolve();
  await Promise.resolve();
  expect(required(document.getElementById('resetSuccess')).hidden).toBe(true);
});

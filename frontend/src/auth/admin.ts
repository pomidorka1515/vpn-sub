import { requiredElement } from '../shared/dom';
import { serverMessage } from '../shared/messages';
import { isRecord } from '../shared/json';
import { button, initAuthChrome, input } from './chrome';

export function initAdminAuth(
  doc: Document = document,
  request: typeof fetch = fetch,
  navigate: (url: string) => void = (url) => {
    window.location.href = url;
  },
): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  const { bootstrap, i18n } = initAuthChrome(doc, window.location, signal);
  const panel = bootstrap.prefix + '/admin';
  const title = requiredElement(doc, 'authTitle');
  const error = requiredElement(doc, 'authError');
  const loginButton = button(doc, 'loginBtn');
  const username = input(doc, 'loginUser');
  const password = input(doc, 'loginPass');
  function showError(message: string): void {
    if (signal.aborted) return;
    error.textContent = message;
    error.classList.add('show');
  }
  async function login(): Promise<void> {
    if (loginButton.disabled) return;
    loginButton.disabled = true;
    try {
      const response = await request(panel + '/session', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.value, password: password.value }),
      });
      const data: unknown = await response.json().catch(() => null);
      if (signal.aborted) return;
      if (response.ok && isRecord(data) && data['success']) navigate(panel);
      else
        showError(
          (isRecord(data) && typeof data['msg'] === 'string' && serverMessage(data['msg'], i18n.language)) ||
            i18n.t('invalid_credentials'),
        );
    } catch {
      showError(i18n.t('network_err'));
    } finally {
      loginButton.disabled = false;
    }
  }
  loginButton.addEventListener(
    'click',
    () => {
      void login();
    },
    { signal },
  );
  for (const field of [username, password])
    field.addEventListener(
      'keydown',
      (event) => {
        if (event.key === 'Enter') void login();
      },
      { signal },
    );
  i18n.applyStaticLang();
  title.textContent = i18n.t('sign_in');
  doc.title = title.textContent;
  void (async () => {
    try {
      const response = await request(panel + '/token', { credentials: 'same-origin' });
      const data: unknown = await response.json().catch(() => null);
      if (
        !signal.aborted &&
        response.ok &&
        isRecord(data) &&
        data['success'] &&
        isRecord(data['obj']) &&
        data['obj']['token']
      )
        navigate(panel);
    } catch {
      /* An unavailable session check leaves the login form usable. */
    }
  })();
  return () => {
    controller.abort();
  };
}

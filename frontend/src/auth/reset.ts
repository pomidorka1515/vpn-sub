import { createApi } from '../shared/api';
import { requiredElement } from '../shared/dom';
import { button, initAuthChrome, input } from './chrome';

export function initPasswordReset(doc: Document = document, request: typeof fetch = fetch): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  const { bootstrap, i18n } = initAuthChrome(doc, window.location, signal);
  const api = createApi(
    () => ({ baseUrl: bootstrap.prefix + '/webapi' }),
    i18n.t,
    () => {},
    request,
  );
  const token = new URLSearchParams(window.location.search).get('token') ?? '';
  const form = requiredElement(doc, 'resetForm');
  const error = requiredElement(doc, 'authError');
  const submit = button(doc, 'resetBtn');
  function showError(message: string): void {
    error.textContent = message;
    error.classList.add('show');
  }
  async function reset(): Promise<void> {
    if (submit.disabled) return;
    const password = input(doc, 'resetPass');
    const confirmation = input(doc, 'resetPass2');
    if (!password.reportValidity() || !confirmation.reportValidity()) return;
    if (password.value !== confirmation.value) {
      showError(i18n.t('passwords_not_match'));
      return;
    }
    submit.disabled = true;
    error.classList.remove('show');
    try {
      const result = await api('POST', '/password-reset?token=' + encodeURIComponent(token), {
        password: password.value,
      });
      if (signal.aborted) return;
      if (result?.success) {
        password.value = '';
        confirmation.value = '';
        form.hidden = true;
        requiredElement(doc, 'resetSuccess').hidden = false;
        window.history.replaceState(window.history.state, '', bootstrap.prefix + '/reset');
      } else showError(result?.msg || i18n.t('network_err'));
    } catch {
      if (!signal.aborted) showError(i18n.t('network_err'));
    } finally {
      submit.disabled = false;
    }
  }
  form.addEventListener(
    'submit',
    (event) => {
      event.preventDefault();
      void reset();
    },
    { signal },
  );
  return () => {
    controller.abort();
  };
}

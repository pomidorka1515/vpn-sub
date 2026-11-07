import type { Translate } from './formatting';

export function createNotifications(doc: Document, translate: Translate, schedule: (callback: () => void, delay: number) => unknown) {
  function toast(message: string, type = 'success'): void {
    doc.querySelector('.toast')?.remove();
    const element = doc.createElement('div');
    element.className = 'toast ' + type;
    element.textContent = message;
    element.setAttribute('role', 'alert');
    element.setAttribute('aria-live', 'polite');
    element.addEventListener('click', () => element.remove());
    doc.body.appendChild(element);
    schedule(() => element.remove(), 2500);
  }
  function logoutWarning(): void {
    let element = doc.getElementById('logoutWarning');
    if (!element) {
      element = doc.createElement('div');
      element.id = 'logoutWarning';
      element.className = 'result-box error';
      element.setAttribute('role', 'alert');
      doc.body.prepend(element);
    }
    element.textContent = translate('logout_unconfirmed');
  }
  return { toast, logoutWarning };
}

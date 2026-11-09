import type { Translate } from './formatting';

export function createNotifications(
  doc: Document,
  translate: Translate,
  schedule: (callback: () => void, delay: number) => number,
  cancel: (handle: number) => void,
) {
  let active: { element: HTMLElement; timer: number } | undefined;
  function clearActive(): void {
    if (!active) return;
    cancel(active.timer);
    active.element.remove();
    active = undefined;
  }
  function toast(message: string, type = 'success'): void {
    clearActive();
    doc.querySelector('.toast')?.remove();
    const element = doc.createElement('div');
    element.className = 'toast ' + type;
    element.textContent = message;
    element.setAttribute('role', 'alert');
    element.setAttribute('aria-live', 'polite');
    element.addEventListener('click', clearActive);
    doc.body.appendChild(element);
    // Native dialogs are above normal stacking contexts, regardless of z-index.
    if (typeof element.showPopover === 'function') {
      element.setAttribute('popover', 'manual');
      element.showPopover();
    }
    const timer = schedule(() => {
      element.remove();
      if (active?.element === element) active = undefined;
    }, 2500);
    active = { element, timer };
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
  return { toast, logoutWarning, dispose: clearActive };
}

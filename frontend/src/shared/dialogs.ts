import { requiredElement } from './dom';
import type { Translate } from './formatting';

export interface DialogOptions {
  input?: boolean;
  inputPlaceholder?: string;
  okText?: string;
  cancelText?: string;
}

export function createDialogs(doc: Document, translate: Translate) {
  let dismissActive: (() => void) | undefined;
  function confirmDialog(message: string, title = '', options: DialogOptions = {}): Promise<boolean | string | null> {
    dismissActive?.();
    const candidateOverlay = requiredElement(doc, 'modalOverlay');
    if (!(candidateOverlay instanceof HTMLDialogElement)) throw new Error(translate('error'));
    const overlay = candidateOverlay;
    const ok = requiredElement(doc, 'modalOk');
    const cancel = requiredElement(doc, 'modalCancel');
    const candidate = doc.getElementById('modalInput');
    const input = candidate instanceof HTMLInputElement ? candidate : null;
    const previousFocus = doc.activeElement;
    requiredElement(doc, 'modalTitle').textContent = title;
    requiredElement(doc, 'modalMsg').textContent = message;
    ok.textContent = options.okText || translate('ok');
    cancel.textContent = options.cancelText || translate('cancel');
    if (input) {
      input.value = '';
      input.style.display = options.input ? 'block' : 'none';
      if (options.input) input.placeholder = options.inputPlaceholder || '';
    }
    return new Promise(resolve => {
      function close(value: boolean | string | null): void {
        overlay.classList.remove('show');
        overlay.close();
        ok.removeEventListener('click', submit);
        cancel.removeEventListener('click', dismiss);
        overlay.removeEventListener('click', outside);
        doc.removeEventListener('keydown', onKey);
        dismissActive = undefined;
        if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
        resolve(value);
      }
      function submit(): void { close(options.input ? input?.value || null : true); }
      function dismiss(): void { close(options.input ? null : false); }
      function outside(event: MouseEvent): void { if (event.target === overlay) dismiss(); }
      function onKey(event: KeyboardEvent): void {
        if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
        else if (event.key === 'Enter') { event.preventDefault(); submit(); }
      }
      dismissActive = dismiss;
      ok.addEventListener('click', submit);
      cancel.addEventListener('click', dismiss);
      overlay.addEventListener('click', outside);
      doc.addEventListener('keydown', onKey);
      overlay.classList.add('show');
      overlay.showModal();
      if (options.input && input) input.focus();
      else cancel.focus();
    });
  }
  return { confirmDialog, dispose: () => dismissActive?.() };
}

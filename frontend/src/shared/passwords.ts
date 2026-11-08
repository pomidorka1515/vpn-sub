import { sharedMessage } from './messages';
import { ICON_EYE, ICON_EYE_OFF } from './icons';

export function togglePass(doc: Document, id: string, button: HTMLElement): void {
  const input = doc.getElementById(id);
  if (!(input instanceof HTMLInputElement)) throw new Error(sharedMessage('missing_element', { id }));
  input.type = input.type === 'password' ? 'text' : 'password';
  button.innerHTML = input.type === 'text' ? ICON_EYE_OFF : ICON_EYE;
}

export function initPassToggles(doc: Document): void {
  doc.querySelectorAll('.pass-toggle').forEach(button => {
    if (!button.querySelector('svg')) button.innerHTML = ICON_EYE;
  });
}

import { sharedMessage } from '../shared/messages';
import { readBootstrap } from '../shared/bootstrap';
import { requiredElement } from '../shared/dom';
import { createI18n } from '../shared/i18n';
import { initPassToggles, togglePass } from '../shared/passwords';
import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';

export function initAuthChrome(doc: Document, location: Location, signal: AbortSignal) {
  const bootstrap = readBootstrap(doc);
  const local = safeStorage(() => localStorage);
  const session = safeStorage(() => sessionStorage);
  const theme = createTheme(doc, local);
  const i18n = createI18n(bootstrap, doc, local, session, location);
  requiredElement(doc, 'themeBtn').addEventListener('click', theme.toggleTheme, { signal });
  doc.querySelectorAll<HTMLElement>('.lang-btn').forEach(button => {
    button.addEventListener('click', () => { i18n.setLang(button.dataset['lang'] ?? ''); }, { signal });
  });
  doc.querySelectorAll<HTMLElement>('.pass-toggle').forEach(button => {
    button.addEventListener('click', () => { togglePass(doc, button.dataset['password'] ?? '', button); }, { signal });
  });
  theme.applyTheme();
  initPassToggles(doc);
  i18n.initLang();
  return { bootstrap, i18n };
}

export function input(doc: Document, id: string): HTMLInputElement {
  const element = requiredElement(doc, id);
  if (!(element instanceof HTMLInputElement)) throw new Error(sharedMessage('missing_element', { id }));
  return element;
}

export function button(doc: Document, id: string): HTMLButtonElement {
  const element = requiredElement(doc, id);
  if (!(element instanceof HTMLButtonElement)) throw new Error(sharedMessage('missing_element', { id }));
  return element;
}

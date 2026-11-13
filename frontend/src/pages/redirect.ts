import { sharedMessage } from '../shared/messages';
import { requiredElement } from '../shared/dom';
import { safeStorage } from '../shared/storage';

export function initRedirect(
  doc: Document = document,
  location: Pick<Location, 'search' | 'replace'> = window.location,
): () => void {
  const params = new URLSearchParams(location.search);
  const requested = params.get('lang');
  const stored = safeStorage(() => localStorage).getItem('lang');
  const lang =
    requested === 'en' || requested === 'ru'
      ? requested
      : stored === 'en' || stored === 'ru'
        ? stored
        : navigator.language.startsWith('ru')
          ? 'ru'
          : 'en';
  doc.documentElement.lang = lang;
  doc.title = sharedMessage('opening', {}, lang);
  requiredElement(doc, 'redirect-label').textContent = sharedMessage('redirecting', {}, lang) + ' ';
  const link = (params.get('prefix') || '') + (params.get('url') || '');
  const manual = requiredElement(doc, 'manual-link');
  if (!(manual instanceof HTMLAnchorElement)) throw new Error(sharedMessage('missing_element', { id: 'manual-link' }));
  manual.href = link;
  manual.textContent = sharedMessage('redirect_link', {}, lang);
  location.replace(link);
  return () => {};
}

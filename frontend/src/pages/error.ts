import { requiredElement } from '../shared/dom';
import { safeStorage } from '../shared/storage';
import { sharedMessage } from '../shared/messages';

export function initError(doc: Document = document, browserHistory: Pick<History, 'length' | 'back'> = history, browserNavigator: Navigator = navigator): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  const storage = safeStorage(() => localStorage);
  // The entry is a blocking head script, before styles or visible content.
  if (storage.getItem('theme') === 'light') doc.documentElement.dataset['theme'] = 'light';
  function render(): void {
    const supported = (value: string | null): value is 'en' | 'ru' => value === 'en' || value === 'ru';
    const requested = new URLSearchParams(doc.location.search).get('lang');
    const stored = [storage.getItem('lang'), storage.getItem('admin_lang')].find(supported);
    const server = supported(doc.documentElement.lang) ? doc.documentElement.lang : undefined;
    const lang = (supported(requested) ? requested : undefined) || stored || server || browserNavigator.languages.map(value => value.split('-')[0]?.toLowerCase() ?? '').find(supported) || 'en';
    doc.documentElement.lang = lang;
    doc.title = sharedMessage('not_found_title', {}, lang);
    requiredElement(doc, 'heading').textContent = sharedMessage('not_found_heading', {}, lang);
    requiredElement(doc, 'description').textContent = sharedMessage('not_found_description', {}, lang);
    requiredElement(doc, 'backLabel').textContent = sharedMessage('go_back', {}, lang);
    const back = requiredElement(doc, 'back');
    if (browserHistory.length > 1) back.addEventListener('click', () => { browserHistory.back(); }, { signal });
    else back.hidden = true;
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', render, { signal, once: true });
  else render();
  return () => { controller.abort(); };
}

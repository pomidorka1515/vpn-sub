import { requiredElement } from '../shared/dom';
import { safeStorage } from '../shared/storage';

export function initError(doc: Document = document, browserHistory: History = history, browserNavigator: Navigator = navigator): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  const storage = safeStorage(() => localStorage);
  // The entry is a blocking head script, before styles or visible content.
  if (storage.getItem('theme') === 'light') doc.documentElement.dataset.theme = 'light';
  function render(): void {
    const supported = (value: string | null): value is 'en' | 'ru' => value === 'en' || value === 'ru';
    const stored = [storage.getItem('lang'), storage.getItem('admin_lang')].find(supported);
    const lang = stored || (browserNavigator.languages || [browserNavigator.language]).map(value => value.split('-')[0]?.toLowerCase() ?? '').find(supported) || 'en';
    doc.documentElement.lang = lang;
    if (lang === 'ru') {
      doc.title = '404 · Страница не найдена';
      requiredElement(doc, 'heading').textContent = 'Страница не найдена';
      requiredElement(doc, 'description').textContent = 'Возможно, страница переехала или в ссылке ошибка. Проверьте адрес и попробуйте снова.';
      requiredElement(doc, 'backLabel').textContent = 'Вернуться назад';
    }
    const back = requiredElement(doc, 'back');
    if (browserHistory.length > 1) back.addEventListener('click', () => browserHistory.back(), { signal });
    else back.hidden = true;
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', render, { signal, once: true });
  else render();
  return () => controller.abort();
}

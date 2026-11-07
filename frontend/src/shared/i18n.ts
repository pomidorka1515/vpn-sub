import type { Bootstrap, Language } from './bootstrap';
import type { StorageAccess } from './storage';

export function createI18n(bootstrap: Bootstrap, doc: Document, local: StorageAccess, session: StorageAccess, location: Pick<Location, 'search' | 'href' | 'reload' | 'replace'>) {
  const language = bootstrap.language;
  const t = (key: string): string => {
    const value = bootstrap.translations[key];
    const fallback = bootstrap.fallback[key];
    return (typeof value === 'string' && value) || (typeof fallback === 'string' && fallback) || key;
  };
  function applyStaticLang(): void {
    doc.querySelectorAll('.lang-btn').forEach(button => {
      button.classList.toggle('active', (button.getAttribute('data-lang') || button.textContent?.toLowerCase()) === language);
    });
    for (const [attribute, target] of [['data-t', 'text'], ['data-t-title', 'title'], ['data-t-aria', 'aria-label'], ['data-t-ph', 'placeholder']]) {
      if (!attribute || !target) continue;
      doc.querySelectorAll(`[${attribute}]`).forEach(element => {
        const value = t(element.getAttribute(attribute) ?? '');
        if (target === 'text') element.textContent = value;
        else element.setAttribute(target, value);
      });
    }
  }
  function setCookie(value: Language): void {
    doc.cookie = 'lang=' + value + '; path=/; max-age=31536000; samesite=lax';
  }
  function initLang(): void {
    const params = new URLSearchParams(location.search);
    const stored = local.getItem('lang') || local.getItem('admin_lang');
    if (!params.has('lang') && (stored === 'en' || stored === 'ru') && stored !== language && !session.getItem('lang_synced')) {
      session.setItem('lang_synced', '1');
      setCookie(stored);
      local.setItem('lang', stored);
      location.reload();
      return;
    }
    local.setItem('lang', language);
    doc.documentElement.lang = language;
    applyStaticLang();
  }
  function setLang(value: string): void {
    if (value !== 'en' && value !== 'ru') return;
    local.setItem('lang', value);
    setCookie(value);
    const url = new URL(location.href);
    url.searchParams.delete('lang');
    location.replace(url.href);
  }
  return { language, t, applyStaticLang, initLang, setLang };
}

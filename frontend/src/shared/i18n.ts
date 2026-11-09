import type { Bootstrap, Language } from './bootstrap';
import type { StorageAccess } from './storage';

export function createI18n(bootstrap: Bootstrap, doc: Document, local: StorageAccess, _session: StorageAccess, location: Pick<Location, 'search' | 'href'>) {
  let language = bootstrap.language;
  const listeners = new Set<() => void>();
  const t = (key: string): string => {
    const value = (bootstrap.languages?.[language] ?? bootstrap.translations)[key];
    const fallback = bootstrap.fallback[key];
    return (typeof value === 'string' && value) || (typeof fallback === 'string' && fallback) || key;
  };
  function applyStaticLang(): void {
    doc.querySelectorAll('.lang-btn').forEach(button => {
      button.classList.toggle('active', (button.getAttribute('data-lang') || button.textContent.toLowerCase()) === language);
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
    if (!params.has('lang') && (stored === 'en' || stored === 'ru') && bootstrap.languages) language = stored;
    local.setItem('lang', language);
    setCookie(language);
    doc.documentElement.lang = language;
    applyStaticLang();
  }
  function setLang(value: string): void {
    if (value !== 'en' && value !== 'ru') return;
    const previous = bootstrap.languages?.[language] ?? bootstrap.translations;
    const next = bootstrap.languages?.[value] ?? bootstrap.translations;
    const replacements = new Map<string, string>();
    for (const [key, text] of Object.entries(previous)) {
      const translated = next[key] || bootstrap.fallback[key];
      if (text && translated && text !== translated) replacements.set(text, translated);
    }
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest('[data-t], script, style, textarea, input, select, option, [contenteditable], .mono, .info-value, .profile-name, .profile-desc') || parent.matches('td')) continue;
      const text = node.textContent ?? '';
      const translated = replacements.get(text.trim());
      if (translated) node.textContent = text.replace(text.trim(), translated);
    }
    doc.querySelectorAll('[title], [aria-label], [placeholder]').forEach(element => {
      for (const attribute of ['title', 'aria-label', 'placeholder']) {
        const text = element.getAttribute(attribute);
        const translated = text && replacements.get(text);
        if (translated) element.setAttribute(attribute, translated);
      }
    });
    language = value;
    local.setItem('lang', value);
    setCookie(value);
    const url = new URL(location.href);
    if (url.searchParams.has('lang')) {
      url.searchParams.delete('lang');
      doc.defaultView?.history.replaceState(doc.defaultView.history.state, '', url.href);
    }
    doc.documentElement.lang = language;
    applyStaticLang();
    listeners.forEach(listener => { listener(); });
  }
  function onChange(listener: () => void): () => void { listeners.add(listener); return () => { listeners.delete(listener); }; }
  return { get language() { return language; }, t, applyStaticLang, initLang, setLang, onChange };
}

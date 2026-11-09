import { sharedMessage } from './messages';

export function displayValue(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint' || typeof value === 'symbol') return String(value);
  if (Array.isArray(value)) return value.map(displayValue).join(',');
  const stringify: unknown = typeof value === 'object' ? Reflect.get(value, 'toString') : undefined;
  if (typeof stringify === 'function') {
    const result: unknown = Reflect.apply(stringify, value, []);
    if (typeof result === 'string') return result;
  }
  return Object.prototype.toString.call(value);
}

export function escapeHtml(value: unknown): string {
  const entities: Record<string, string> = {
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  };
  return displayValue(value).replace(/[&<>"']/g, character => entities[character] ?? character);
}

export function requiredElement(doc: Document, id: string): HTMLElement {
  const element = doc.getElementById(id);
  if (!element) throw new Error(sharedMessage('missing_element', { id }));
  return element;
}

export function loadingHtml(translate: (key: string) => string): string {
  return '<div class="loading" role="status"><span class="spinner" aria-hidden="true"></span>' + escapeHtml(translate('loading')) + '</div>';
}

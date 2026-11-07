export function escapeHtml(value: unknown): string {
  const entities: Record<string, string> = {
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  };
  return String(value ?? '').replace(/[&<>"']/g, character => entities[character] ?? character);
}

export function requiredElement(doc: Document, id: string): HTMLElement {
  const element = doc.getElementById(id);
  if (!element) throw new Error(`Missing element #${id}`);
  return element;
}

export function loadingHtml(translate: (key: string) => string): string {
  return '<div class="loading" role="status"><span class="spinner" aria-hidden="true"></span>' + escapeHtml(translate('loading')) + '</div>';
}

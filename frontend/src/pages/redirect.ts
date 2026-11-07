import { requiredElement } from '../shared/dom';

export function initRedirect(doc: Document = document, location: Pick<Location, 'search' | 'replace'> = window.location): () => void {
  const params = new URLSearchParams(location.search);
  const link = (params.get('prefix') || '') + (params.get('url') || '');
  const manual = requiredElement(doc, 'manual-link');
  if (!(manual instanceof HTMLAnchorElement)) throw new Error('Expected link #manual-link');
  manual.href = link;
  location.replace(link);
  return () => {};
}

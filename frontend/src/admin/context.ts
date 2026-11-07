import type { ApiResult } from '../shared/api';
import type { DialogOptions } from '../shared/dialogs';
import type { Translate } from '../shared/formatting';

export interface AdminContext {
  doc: Document;
  t: Translate;
  api: (method: string, path: string, body?: unknown) => Promise<ApiResult | null>;
  toast: (message: string, kind?: string) => void;
  confirm: (message: string, title?: string, options?: DialogOptions) => Promise<boolean | string | null>;
  loadGen: (key: string) => () => boolean;
}

export function element(doc: Document, id: string): HTMLElement {
  const result = doc.getElementById(id);
  if (!result) throw new Error(`Missing admin element #${id}`);
  return result;
}

export function input(doc: Document, id: string): HTMLInputElement | HTMLSelectElement {
  const result = element(doc, id);
  if (!(result instanceof HTMLInputElement) && !(result instanceof HTMLSelectElement)) throw new Error(`Expected input #${id}`);
  return result;
}

export function button(doc: Document, id: string): HTMLButtonElement {
  const result = element(doc, id);
  if (!(result instanceof HTMLButtonElement)) throw new Error(`Expected button #${id}`);
  return result;
}

export function checkbox(doc: Document, id: string): HTMLInputElement {
  const result = element(doc, id);
  if (!(result instanceof HTMLInputElement) || result.type !== 'checkbox') throw new Error(`Expected checkbox #${id}`);
  return result;
}

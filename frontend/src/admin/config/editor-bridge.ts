import { sharedMessage } from '../../shared/messages';
import type { Translate } from '../../shared/formatting';
import { createJsonEditors } from './json-editor';

let active: ReturnType<typeof createJsonEditors> | undefined;

export function initJsonEditors(doc: Document, url: string, translate: Translate): () => void {
  if (active) throw new Error(sharedMessage('editors_initialized'));
  const editors = createJsonEditors(doc, url, translate);
  active = editors;
  return () => { if (active === editors) active = undefined; editors.dispose(); };
}

export function destroyJsonEditors(root: Node | null): void { if (root) active?.destroy(root); }
export function refreshJsonEditors(root: Node | null): void { if (root) active?.refresh(root); }
export function mountJsonEditor(textarea: HTMLTextAreaElement, options: { parse: (text: string) => unknown; format: (text: string) => string; label: string }): Promise<void> {
  return active?.mount(textarea, options) ?? Promise.resolve();
}

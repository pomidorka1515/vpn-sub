import { sharedMessage } from '../../shared/messages';
import type { Translate } from '../../shared/formatting';
import { isRecord } from '../../shared/json';

interface Position { line: number; ch: number }
interface Marker { clear(): void }
interface CodeMirrorView {
  getValue(): string;
  setValue(value: string): void;
  focus(): void;
  refresh(): void;
  somethingSelected(): boolean;
  indentSelection(direction: string): void;
  replaceSelection(text: string, selection: string): void;
  posFromIndex(index: number): Position;
  markText(from: Position, to: Position, options: { className: string; title: string }): Marker;
  on(event: 'change', listener: () => void): void;
}
type CodeMirrorFactory = (mount: HTMLElement, options: {
  value: string;
  mode: { name: string; json: boolean };
  lineNumbers: boolean;
  indentUnit: number;
  tabSize: number;
  indentWithTabs: boolean;
  matchBrackets: boolean;
  autoCloseBrackets: boolean;
  screenReaderLabel: string;
  extraKeys: { Tab: (view: CodeMirrorView) => void };
}) => CodeMirrorView;

interface Instance { destroy(): void; refresh?(): void }
interface Options { parse: (text: string) => unknown; format: (text: string) => string; label: string }

function isEditorView(value: unknown): value is CodeMirrorView {
  return isRecord(value) && ['getValue', 'setValue', 'focus', 'refresh', 'somethingSelected', 'indentSelection', 'replaceSelection', 'posFromIndex', 'markText', 'on'].every(key => typeof value[key] === 'function');
}

// The editor library stays lazy and optional; a failed load leaves the textarea usable.
export function createJsonEditors(doc: Document, url: string, t: Translate) {
  const instances = new Map<HTMLTextAreaElement, Instance>();
  let loading: Promise<CodeMirrorFactory> | undefined;
  let disposed = false;
  const isDisposed = (): boolean => disposed;
  function loadEditor(): Promise<CodeMirrorFactory> {
    return loading ??= new Promise((resolve, reject) => {
      const script = doc.createElement('script');
      script.src = url;
      script.onload = () => {
        const library: unknown = Reflect.get(doc.defaultView ?? globalThis, 'CodeMirror');
        if (typeof library !== 'function') { loading = undefined; reject(new Error(sharedMessage('editor_unavailable'))); return; }
        resolve((mount, options) => {
          const view: unknown = Reflect.apply(library, undefined, [mount, options]);
          if (!isEditorView(view)) throw new Error(sharedMessage('editor_unavailable'));
          return view;
        });
      };
      script.onerror = () => { script.remove(); loading = undefined; reject(new Error(sharedMessage('editor_unavailable'))); };
      doc.head.append(script);
    });
  }
  function destroy(root: Node): void {
    for (const [textarea, instance] of instances) {
      if (!root.contains(textarea)) continue;
      instance.destroy();
      instances.delete(textarea);
    }
  }
  function refresh(root: Node): void {
    for (const [textarea, instance] of instances) {
      if (root.contains(textarea)) instance.refresh?.();
    }
  }
  async function mount(textarea: HTMLTextAreaElement, { parse, format, label }: Options): Promise<void> {
    const readonly = (): boolean => textarea.disabled || textarea.readOnly;
    if (disposed || instances.has(textarea) || readonly()) return;
    const pending: Instance = { destroy() {} };
    instances.set(textarea, pending);
    let factory: CodeMirrorFactory;
    try { factory = await loadEditor(); }
    catch { if (instances.get(textarea) === pending) instances.delete(textarea); return; }
    if (isDisposed() || !textarea.isConnected || instances.get(textarea) !== pending) return;
    if (readonly()) { instances.delete(textarea); return; }
    const wrapper = doc.createElement('div');
    wrapper.className = 'config-json-editor';
    const toolbar = doc.createElement('div');
    toolbar.className = 'config-json-toolbar';
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'btn-tiny';
    button.dataset['t'] = 'config_format_json';
    button.textContent = t('config_format_json');
    const status = doc.createElement('span');
    status.className = 'config-json-status';
    status.setAttribute('aria-live', 'polite');
    toolbar.append(button, status);
    wrapper.append(toolbar);
    textarea.after(wrapper);
    let view: CodeMirrorView;
    try {
      view = factory(wrapper, {
        value: textarea.value, mode: { name: 'javascript', json: true },
        lineNumbers: true, indentUnit: 2, tabSize: 2, indentWithTabs: false,
        matchBrackets: true, autoCloseBrackets: true, screenReaderLabel: label,
        extraKeys: { Tab: cm => { if (cm.somethingSelected()) cm.indentSelection('add'); else cm.replaceSelection('  ', 'end'); } },
      });
    } catch {
      wrapper.remove();
      if (instances.get(textarea) === pending) instances.delete(textarea);
      loading = undefined;
      return;
    }
    let marker: Marker | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const validate = (): void => {
      marker?.clear();
      marker = null;
      try {
        parse(view.getValue());
        status.dataset['t'] = 'config_json_valid';
        status.textContent = t('config_json_valid');
        status.classList.remove('error');
        button.disabled = false;
      } catch (error) {
        delete status.dataset['t'];
        const position: unknown = error instanceof Error ? Reflect.get(error, 'position') : 0;
        const index = Math.max(0, Math.min(typeof position === 'number' ? position : 0, view.getValue().length));
        const pos = view.posFromIndex(index);
        status.textContent = `${t('config_err_json')} (${String(pos.line + 1)}:${String(pos.ch + 1)})`;
        status.classList.add('error');
        button.disabled = true;
        marker = view.markText(pos, view.posFromIndex(index + 1), { className: 'config-json-invalid', title: status.textContent });
      }
    };
    view.on('change', () => {
      textarea.value = view.getValue();
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      clearTimeout(timer);
      timer = setTimeout(validate, 250);
    });
    button.addEventListener('click', () => {
      try { view.setValue(format(view.getValue())); view.focus(); validate(); }
      catch { validate(); }
    });
    textarea.style.display = 'none';
    validate();
    instances.set(textarea, {
      refresh() { view.refresh(); validate(); },
      destroy() { clearTimeout(timer); marker?.clear(); wrapper.remove(); textarea.style.display = ''; },
    });
  }
  return { mount, refresh, destroy, dispose: (): void => { disposed = true; destroy(doc); } };
}

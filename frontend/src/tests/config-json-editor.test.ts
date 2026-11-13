import { required } from './required';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createJsonEditors } from '../admin/config/json-editor';
import { parseConfigJson, stringifyConfig } from '../admin/config/json';

let cleanup: (() => void) | undefined;
afterEach(() => {
  cleanup?.();
  cleanup = undefined;
});
function setup() {
  document.body.innerHTML = '<div id="root"><textarea>{"a":1}</textarea></div>';
  const textarea = required(document.querySelector('textarea'));
  const editors = createJsonEditors(document, '/editor.js', (key) => key);
  cleanup = editors.dispose;
  const options = {
    parse: parseConfigJson,
    format: (text: string) => stringifyConfig(parseConfigJson(text), true),
    label: 'json',
  };
  return { textarea, editors, options };
}

describe('optional JSON editor', () => {
  it('keeps the textarea usable if the library fails and permits retrying', async () => {
    const { textarea, editors, options } = setup();
    const pending = editors.mount(textarea, options);
    required(document.querySelector<HTMLScriptElement>('script[src="/editor.js"]')).dispatchEvent(new Event('error'));
    await pending;
    expect(textarea.style.display).toBe('');
    expect(document.querySelector('.config-json-editor')).toBeNull();
    const retry = editors.mount(textarea, options);
    expect(document.querySelector('script[src="/editor.js"]')).not.toBeNull();
    required(document.querySelector<HTMLScriptElement>('script[src="/editor.js"]')).dispatchEvent(new Event('error'));
    await retry;
  });
  it('keeps the textarea usable if the loaded editor has an invalid interface', async () => {
    const { textarea, editors, options } = setup();
    vi.stubGlobal(
      'CodeMirror',
      vi.fn(() => ({})),
    );
    const pending = editors.mount(textarea, options);
    required(document.querySelector<HTMLScriptElement>('script[src="/editor.js"]')).dispatchEvent(new Event('load'));
    await pending;
    expect(textarea.style.display).toBe('');
    expect(document.querySelector('.config-json-editor')).toBeNull();
    const retry = editors.mount(textarea, options);
    const scripts = document.querySelectorAll<HTMLScriptElement>('script[src="/editor.js"]');
    required(scripts[scripts.length - 1]).dispatchEvent(new Event('error'));
    await retry;
    expect(textarea.style.display).toBe('');
  });
  it('synchronizes edits, validates errors, formats JSON and cleans up the marker', async () => {
    const { textarea, editors, options } = setup();
    let value = textarea.value;
    let onChange = () => {};
    const marker = { clear: vi.fn() };
    const view = {
      getValue: () => value,
      setValue: (next: string) => {
        value = next;
        onChange();
      },
      focus: vi.fn(),
      refresh: vi.fn(),
      somethingSelected: () => false,
      indentSelection: vi.fn(),
      replaceSelection: vi.fn(),
      posFromIndex: (ch: number) => ({ line: 0, ch }),
      markText: vi.fn(() => marker),
      on: (_event: string, listener: () => void) => {
        onChange = listener;
      },
    };
    vi.stubGlobal(
      'CodeMirror',
      vi.fn(() => view),
    );
    const pending = editors.mount(textarea, options);
    required(document.querySelector<HTMLScriptElement>('script[src="/editor.js"]')).dispatchEvent(new Event('load'));
    await pending;
    expect(textarea.style.display).toBe('none');
    const status = required(document.querySelector<HTMLElement>('.config-json-status'));
    const button = required(document.querySelector<HTMLButtonElement>('.config-json-toolbar button'));
    expect(status.textContent).toBe('config_json_valid');
    vi.useFakeTimers();
    value = '{bad';
    onChange();
    expect(textarea.value).toBe('{bad');
    await vi.advanceTimersByTimeAsync(250);
    expect(status.classList.contains('error')).toBe(true);
    expect(button.disabled).toBe(true);
    expect(view.markText).toHaveBeenCalledOnce();
    value = '{"a":2}';
    onChange();
    await vi.advanceTimersByTimeAsync(250);
    expect(marker.clear).toHaveBeenCalled();
    button.click();
    expect(textarea.value).toBe('{\n  "a": 2\n}');
    expect(view.focus).toHaveBeenCalledOnce();
    editors.refresh(document.body);
    expect(view.refresh).toHaveBeenCalledOnce();
    editors.destroy(document.body);
    expect(textarea.style.display).toBe('');
    expect(document.querySelector('.config-json-editor')).toBeNull();
  });
  it('does not mount after disposal while the library is loading', async () => {
    const { textarea, editors, options } = setup();
    const factory = vi.fn();
    vi.stubGlobal('CodeMirror', factory);
    const pending = editors.mount(textarea, options);
    editors.dispose();
    required(document.querySelector<HTMLScriptElement>('script[src="/editor.js"]')).dispatchEvent(new Event('load'));
    await pending;
    expect(factory).not.toHaveBeenCalled();
    expect(textarea.style.display).toBe('');
  });
});

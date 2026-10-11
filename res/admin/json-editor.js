// Frozen local distribution; no build step.
const instances = new Map();
let loading;

function loadEditor() {
  return loading ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const url = new URL('../vendor/codemirror-5.65.16.min.js', import.meta.url);
    url.search = new URL(import.meta.url).search;
    script.src = url.href;
    script.onload = () => resolve(window.CodeMirror);
    script.onerror = () => { script.remove(); loading = null; reject(new Error('Editor unavailable')); };
    document.head.append(script);
  });
}

export function destroyJsonEditors(root) {
  for (const [textarea, instance] of instances) {
    if (!root.contains(textarea)) continue;
    instance.destroy();
    instances.delete(textarea);
  }
}

export function refreshJsonEditors(root) {
  for (const [textarea, instance] of instances) {
    if (root.contains(textarea)) instance.refresh?.();
  }
}

export async function mountJsonEditor(textarea, { parse, format, label }) {
  if (instances.has(textarea)) return;
  const pending = { destroy() {} };
  instances.set(textarea, pending);
  let CodeMirror;
  try { CodeMirror = await loadEditor(); } catch {
    if (instances.get(textarea) === pending) instances.delete(textarea);
    return; // Keep the textarea usable if the local asset cannot load.
  }
  if (!textarea.isConnected || instances.get(textarea) !== pending) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'config-json-editor';
  const toolbar = document.createElement('div');
  toolbar.className = 'config-json-toolbar';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn-tiny';
  button.dataset.t = 'config_format_json';
  button.textContent = t('config_format_json');
  const status = document.createElement('span');
  status.className = 'config-json-status';
  status.setAttribute('aria-live', 'polite');
  toolbar.append(button, status);
  wrapper.append(toolbar);
  textarea.after(wrapper);
  const view = CodeMirror(wrapper, {
    value: textarea.value, mode: { name: 'javascript', json: true },
    lineNumbers: true, indentUnit: 2, tabSize: 2, indentWithTabs: false,
    matchBrackets: true, autoCloseBrackets: true, screenReaderLabel: label,
    extraKeys: { Tab: cm => cm.somethingSelected() ? cm.indentSelection('add') : cm.replaceSelection('  ', 'end') },
  });
  let marker, timer;
  const validate = () => {
    marker?.clear();
    marker = null;
    try {
      parse(view.getValue());
      status.dataset.t = 'config_json_valid';
      status.textContent = t('config_json_valid');
      status.classList.remove('error');
      button.disabled = false;
    } catch (error) {
      delete status.dataset.t;
      const index = Math.max(0, Math.min(error.position ?? 0, view.getValue().length));
      const pos = view.posFromIndex(index);
      status.textContent = `${t('config_err_json')} (${pos.line + 1}:${pos.ch + 1})`;
      status.classList.add('error');
      button.disabled = true;
      marker = view.markText(pos, view.posFromIndex(index + 1), {
        className: 'config-json-invalid', title: status.textContent,
      });
    }
  };
  view.on('change', () => {
    textarea.value = view.getValue();
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    clearTimeout(timer);
    timer = setTimeout(validate, 250);
  });
  button.onclick = () => {
    try { view.setValue(format(view.getValue())); view.focus(); validate(); }
    catch { validate(); }
  };
  textarea.style.display = 'none';
  validate();
  instances.set(textarea, {
    refresh() { view.refresh(); validate(); },
    destroy() {
      clearTimeout(timer);
      marker?.clear();
      wrapper.remove();
      textarea.style.display = '';
    },
  });
}

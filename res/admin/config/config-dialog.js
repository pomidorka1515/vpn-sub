import { destroyJsonEditors } from '../json-editor.js';
import { editor, INTEGER_RE } from './config-state.js';
import { ConfigNumber, clone, parseJsonField } from './config-json.js';
import { passWrap, fieldError, setError, enhanceJsonInputs, rowList, readRows, bindRows, bilingual, nodeOptions, readText } from './config-controls.js';
import { validateDraft } from './config-validation.js';

export function createConfigDialog({ markDirty, renderCards }) {
  return function openNew(kind) {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const overlay = document.getElementById('configEditorOverlay');
    const body = document.getElementById('configEditorBody');
    const title = document.getElementById('configEditorTitle');
    const opener = document.getElementById('cfg-new-' + kind);
    title.textContent = t('config_new');
    const draft = kind === 'inbounds' ? newInbound() : newPanel();
    editor.dialog = { kind, draft, opener };
    body.innerHTML = kind === 'inbounds' ? dialogInbound(draft) : dialogPanel(draft);
    if (kind === 'panels') {
      const repaintRows = (id, rows, rowKind) => {
        const root = document.getElementById(id);
        root.innerHTML = new DOMParser().parseFromString(rowList('unused', rows, rowKind), 'text/html').getElementById('unused').innerHTML;
      };
      bindRows('cfg-dlg-inbounds', 'int', (rows, live) => { if (!live) repaintRows('cfg-dlg-inbounds', rows, 'int'); });
      const nested = which => {
        const slot = body.querySelector(`[data-dialog-nested="${which}"]`);
        if (which === 'nginx_auth') slot.innerHTML = `<div class="config-pair"><input type="text" data-field="nginx0">${passWrap('cfg-dlg-nginx-password', '')}</div>`;
        else {
          slot.innerHTML = rowList('cfg-dlg-headers', [], 'pair');
          bindRows('cfg-dlg-headers', 'pair', (rows, live) => { if (!live) repaintRows('cfg-dlg-headers', rows, 'pair'); });
        }
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'btn-tiny'; remove.textContent = t('config_remove');
        remove.onclick = () => { slot.innerHTML = ''; addNestedButton(slot, which); };
        slot.appendChild(remove);
        initPassToggles();
      };
      const addNestedButton = (slot, which) => {
        const add = document.createElement('button');
        add.type = 'button'; add.className = 'btn-tiny'; add.textContent = t('config_add');
        add.onclick = () => nested(which); slot.appendChild(add);
      };
      ['nginx_auth', 'inject_headers'].forEach(which => addNestedButton(body.querySelector(`[data-dialog-nested="${which}"]`), which));
    }
    initPassToggles();
    overlay.hidden = false;
    overlay.classList.add('show');
    if (kind === 'inbounds') enhanceJsonInputs(body);
    body.querySelector('input, textarea, select')?.focus();
    const close = (created) => {
      destroyJsonEditors(body);
      overlay.classList.remove('show');
      overlay.hidden = true;
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', onKey);
      overlay.onclick = null;
      editor.dialog = null;
      if (!created) opener?.focus();
    };
    const submit = () => {
      const next = kind === 'inbounds' ? readDialogInbound(body) : readDialogPanel(body);
      const probe = clone(editor.draft);
      const id = next.id.trim();
      const map = kind === 'inbounds' ? probe.profiles : probe['3xui'];
      if (!id || Object.prototype.hasOwnProperty.call(map, id)) {
        setError('cfg-dialog-err', t(!id ? 'config_err_required' : 'config_err_unique'));
        return;
      }
      if (kind === 'panels') {
        const keys = readRows('cfg-dlg-headers', 'pair').map(row => row[0]);
        if (new Set(keys).size !== keys.length) { setError('cfg-dialog-err', t('config_err_unique')); return; }
      }
      const stored = { ...next };
      delete stored.id;
      Object.defineProperty(map, id, { value: stored, enumerable: true, writable: true, configurable: true });
      const prefix = (kind === 'inbounds' ? 'profiles.' : '3xui.') + id;
      const issues = validateDraft(probe).filter(item => item.path === prefix || item.path.startsWith(prefix + '.'));
      if (issues.length) {
        setError('cfg-dialog-err', issues[0].message);
        return;
      }
      const target = kind === 'inbounds' ? editor.draft.profiles : editor.draft['3xui'];
      Object.defineProperty(target, id, { value: stored, enumerable: true, writable: true, configurable: true });
      close(true);
      renderCards(kind);
      markDirty();
      document.getElementById('cfg-new-' + kind)?.focus();
    };
    const onKey = event => {
      if (event.key === 'Escape') close(false);
      else if (event.key === 'Tab') {
        const controls = [...overlay.querySelectorAll('input:not([type="hidden"]), textarea, select, button')].filter(el => !el.disabled && el.getClientRects().length);
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      } else if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA' && event.target.tagName !== 'BUTTON') {
        event.preventDefault();
        submit();
      }
    };
    document.getElementById('configEditorCancel').onclick = () => close(false);
    document.getElementById('configEditorOk').onclick = submit;
    overlay.onclick = event => { if (event.target === overlay) close(false); };
    document.addEventListener('keydown', onKey);
  };
}

function newInbound() {
  const nodes = Object.keys(editor.draft.nodes || {});
  return {
    id: '', flag: '', name: ['', ''], description: ['', ''], shortProfileDescription: ['', ''],
    whitelist: false, node: nodes[0] || '', masterLink: '', json: {}, xhttpExtra: {},
  };
}

function newPanel() {
  return {
    id: '', name: '', address: '127.0.0.1', uri: '', port: '', token: '',
    https: false, whitelist: false, mode: 'blacklist', inbounds_list: [],
  };
}

function dialogInbound(draft) {
  return `<div class="form-grid">
    <div class="form-row"><label>id</label><div class="input-wrap"><input type="text" data-field="id" spellcheck="false"></div></div>
    <div class="form-row"><label>flag</label><div class="input-wrap"><input type="text" data-field="flag"></div></div>
    <div class="form-row"><label>name</label><div class="input-wrap">${bilingual('cfg-dlg-name', draft.name)}</div></div>
    <div class="form-row"><label>description</label><div class="input-wrap">${bilingual('cfg-dlg-description', draft.description)}</div></div>
    <div class="form-row"><label>shortProfileDescription</label><div class="input-wrap">${bilingual('cfg-dlg-short', draft.shortProfileDescription)}</div></div>
    <div class="form-row"><label>whitelist</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist"><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>node</label><div class="input-wrap"><select data-field="node">${nodeOptions(draft.node)}</select></div></div>
    <div class="form-row"><label>masterLink</label><div class="input-wrap"><input type="text" data-field="masterLink" spellcheck="false"></div></div>
    <div class="form-row"><label>json</label><div class="input-wrap"><textarea class="config-json" data-field="json" spellcheck="false">{}</textarea></div></div>
    <div class="form-row"><label>xhttpExtra</label><div class="input-wrap"><textarea class="config-json" data-field="xhttpExtra" spellcheck="false">{}</textarea></div></div>
    ${fieldError('cfg-dialog-err')}
  </div>`;
}

function dialogPanel() {
  return `<div class="form-grid">
    <div class="form-row"><label>key</label><div class="input-wrap"><input type="text" data-field="id" spellcheck="false"></div></div>
    <div class="form-row"><label>name</label><div class="input-wrap"><input type="text" data-field="name"></div></div>
    <div class="form-row"><label>address</label><div class="input-wrap"><input type="text" data-field="address" value="127.0.0.1" spellcheck="false"></div></div>
    <div class="form-row"><label>uri</label><div class="input-wrap"><input type="text" data-field="uri" spellcheck="false"></div></div>
    <div class="form-row"><label>port</label><div class="input-wrap"><input type="text" data-field="port" spellcheck="false"></div></div>
    <div class="form-row"><label>token</label><div class="input-wrap">${passWrap('cfg-dlg-token', '')}</div></div>
    <div class="form-row"><label>https</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="https"><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>whitelist</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist"><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>mode</label><div class="input-wrap"><select data-field="mode"><option value="blacklist" selected>blacklist</option><option value="whitelist">whitelist</option></select></div></div>
    <div class="form-row"><label>inbounds_list</label><div class="input-wrap">${rowList('cfg-dlg-inbounds', [], 'int')}</div></div>
    <div class="form-row"><label>nginx_auth</label><div class="input-wrap" data-dialog-nested="nginx_auth"></div></div>
    <div class="form-row"><label>inject_headers</label><div class="input-wrap" data-dialog-nested="inject_headers"></div></div>
    ${fieldError('cfg-dialog-err')}
  </div>`;
}

function dialogValue(body, field) {
  return body.querySelector(`[data-field="${field}"]`);
}

function readDialogInbound(body) {
  const json = parseJsonField(dialogValue(body, 'json')?.value, {});
  const extra = parseJsonField(dialogValue(body, 'xhttpExtra')?.value, {});
  return {
    id: dialogValue(body, 'id')?.value ?? '',
    flag: dialogValue(body, 'flag')?.value ?? '',
    name: [document.getElementById('cfg-dlg-name-0')?.value ?? '', document.getElementById('cfg-dlg-name-1')?.value ?? ''],
    description: [document.getElementById('cfg-dlg-description-0')?.value ?? '', document.getElementById('cfg-dlg-description-1')?.value ?? ''],
    shortProfileDescription: [document.getElementById('cfg-dlg-short-0')?.value ?? '', document.getElementById('cfg-dlg-short-1')?.value ?? ''],
    whitelist: !!dialogValue(body, 'whitelist')?.checked,
    node: dialogValue(body, 'node')?.value ?? '',
    masterLink: dialogValue(body, 'masterLink')?.value ?? '',
    json, xhttpExtra: extra,
  };
}

function readDialogPanel(body) {
  const port = dialogValue(body, 'port')?.value ?? '';
  const result = {
    id: dialogValue(body, 'id')?.value ?? '',
    name: dialogValue(body, 'name')?.value ?? '',
    address: dialogValue(body, 'address')?.value ?? '',
    uri: dialogValue(body, 'uri')?.value ?? '',
    port: INTEGER_RE.test(port) ? new ConfigNumber(port) : port,
    token: document.getElementById('cfg-dlg-token')?.value ?? '',
    https: !!dialogValue(body, 'https')?.checked,
    whitelist: !!dialogValue(body, 'whitelist')?.checked,
    mode: dialogValue(body, 'mode')?.value || 'blacklist',
    inbounds_list: readRows('cfg-dlg-inbounds', 'int').map(raw => INTEGER_RE.test(raw.trim()) ? new ConfigNumber(raw.trim()) : raw),
  };
  if (dialogValue(body, 'nginx0')) result.nginx_auth = [dialogValue(body, 'nginx0').value, readText('cfg-dlg-nginx-password')];
  if (document.getElementById('cfg-dlg-headers')) result.inject_headers = Object.fromEntries(readRows('cfg-dlg-headers', 'pair'));
  return result;
}

import { sharedMessage } from '../../shared/messages';
import type { Translate } from '../../shared/formatting';
import { escapeHtml } from '../../shared/dom';
import { initPassToggles } from '../../shared/passwords';
import { destroyJsonEditors } from './editor-bridge';
import { editor, INTEGER_RE } from './state';
import { ConfigNumber, clone, isPlainObject, parseJsonField } from './json';
import type { ConfigObject } from './json';
import { passWrap, fieldError, setError, enhanceJsonInputs, rowList, readRows, bindRows, bilingual, nodeOptions, readText } from './controls';
import { validateDraft } from './validation';

let t: Translate = key => key;
export function initConfigDialog(translate: Translate): () => void { t = translate; return () => { t = key => key; }; }
const required = (id: string): HTMLElement => { const element = document.getElementById(id); if (!element) throw new Error(sharedMessage('missing_element', { id })); return element; };
type Kind = 'inbounds' | 'panels';

export function createConfigDialog({ markDirty, renderCards }: { markDirty(): void; renderCards(kind: Kind): void }) {
  let closeActive: ((created: boolean) => void) | null = null;
  function openNew(kind: Kind): void {
    closeActive?.(false);
    const listeners = new AbortController();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const overlay = required('configEditorOverlay');
    const body = required('configEditorBody');
    const title = required('configEditorTitle');
    const opener = document.getElementById('cfg-new-' + kind);
    title.textContent = t('config_new');
    const draft = kind === 'inbounds' ? newInbound() : newPanel();
    editor.dialog = { kind, draft, opener };
    body.innerHTML = kind === 'inbounds' ? dialogInbound(draft as ReturnType<typeof newInbound>) : dialogPanel();
    if (kind === 'panels') {
      const repaintRows = (id: string, rows: Array<string | string[]>, rowKind: string): void => {
        const root = required(id);
        root.innerHTML = new DOMParser().parseFromString(rowList('unused', rows, rowKind), 'text/html').getElementById('unused')?.innerHTML ?? '';
      };
      bindRows('cfg-dlg-inbounds', 'int', (rows, live) => { if (!live) repaintRows('cfg-dlg-inbounds', rows, 'int'); });
      const nested = (which: string): void => {
        const slot = body.querySelector<HTMLElement>(`[data-dialog-nested="${which}"]`);
        if (!slot) return;
        if (which === 'nginx_auth') slot.innerHTML = `<div class="config-pair"><input type="text" data-field="nginx0">${passWrap('cfg-dlg-nginx-password', '')}</div>`;
        else {
          slot.innerHTML = rowList('cfg-dlg-headers', [], 'pair');
          bindRows('cfg-dlg-headers', 'pair', (rows, live) => { if (!live) repaintRows('cfg-dlg-headers', rows, 'pair'); });
        }
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'btn-tiny'; remove.textContent = t('config_remove');
        remove.addEventListener('click', () => { slot.innerHTML = ''; addNestedButton(slot, which); });
        slot.appendChild(remove);
        initPassToggles(document);
      };
      const addNestedButton = (slot: HTMLElement | null, which: string): void => {
        if (!slot) return;
        const add = document.createElement('button');
        add.type = 'button'; add.className = 'btn-tiny'; add.textContent = t('config_add');
        add.addEventListener('click', () => nested(which)); slot.appendChild(add);
      };
      ['nginx_auth', 'inject_headers'].forEach(which => addNestedButton(body.querySelector(`[data-dialog-nested="${which}"]`), which));
    }
    initPassToggles(document);
    overlay.hidden = false;
    overlay.classList.add('show');
    if (kind === 'inbounds') enhanceJsonInputs(body);
    body.querySelector<HTMLElement>('input, textarea, select')?.focus();
    const close = (created: boolean): void => {
      destroyJsonEditors(body);
      overlay.classList.remove('show');
      overlay.hidden = true;
      document.body.style.overflow = previousOverflow;
      listeners.abort();
      editor.dialog = null;
      if (closeActive === close) closeActive = null;
      if (!created) opener?.focus();
    };
    closeActive = close;
    const submit = () => {
      const next = kind === 'inbounds' ? readDialogInbound(body) : readDialogPanel(body);
      if (!editor.draft) return;
      const probe = clone(editor.draft);
      const id = next.id.trim();
      const map = kind === 'inbounds' ? probe.profiles : probe['3xui'];
      if (!isPlainObject(map)) return;
      if (!id || Object.prototype.hasOwnProperty.call(map, id)) {
        setError('cfg-dialog-err', t(!id ? 'config_err_required' : 'config_err_unique'));
        return;
      }
      if (kind === 'panels') {
        const keys = readRows('cfg-dlg-headers', 'pair').map(row => row[0]);
        if (new Set(keys).size !== keys.length) { setError('cfg-dialog-err', t('config_err_unique')); return; }
      }
      const { id: _id, ...stored } = next;
      Object.defineProperty(map, id, { value: stored, enumerable: true, writable: true, configurable: true });
      const prefix = (kind === 'inbounds' ? 'profiles.' : '3xui.') + id;
      const issues = validateDraft(probe).filter(item => item.path === prefix || item.path.startsWith(prefix + '.'));
      if (issues.length) {
        setError('cfg-dialog-err', issues[0]?.message ?? '');
        return;
      }
      const target = kind === 'inbounds' ? editor.draft.profiles : editor.draft['3xui'];
      if (!isPlainObject(target)) return;
      Object.defineProperty(target, id, { value: stored, enumerable: true, writable: true, configurable: true });
      close(true);
      renderCards(kind);
      markDirty();
      document.getElementById('cfg-new-' + kind)?.focus();
    };
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') close(false);
      else if (event.key === 'Tab') {
        const controls = [...overlay.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | HTMLButtonElement>('input:not([type="hidden"]), textarea, select, button')].filter(el => !el.disabled && el.getClientRects().length);
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      } else if (event.key === 'Enter' && event.target instanceof HTMLElement && event.target.tagName !== 'TEXTAREA' && event.target.tagName !== 'BUTTON') {
        event.preventDefault();
        submit();
      }
    };
    required('configEditorCancel').addEventListener('click', () => close(false), { signal: listeners.signal });
    required('configEditorOk').addEventListener('click', submit, { signal: listeners.signal });
    overlay.addEventListener('click', event => { if (event.target === overlay) close(false); }, { signal: listeners.signal });
    document.addEventListener('keydown', onKey, { signal: listeners.signal });
  }
  openNew.dispose = () => closeActive?.(false);
  return openNew;
}

function newInbound() {
  const nodes = Object.keys(isPlainObject(editor.draft?.nodes) ? editor.draft.nodes : {});
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

function dialogInbound(draft: ReturnType<typeof newInbound>): string {
  return `<div class="form-grid">
    <div class="form-row"><label>${escapeHtml(t('config_field_id'))}</label><div class="input-wrap"><input type="text" data-field="id" spellcheck="false"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_flag'))}</label><div class="input-wrap"><input type="text" data-field="flag"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_name'))}</label><div class="input-wrap">${bilingual('cfg-dlg-name', draft.name)}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_description'))}</label><div class="input-wrap">${bilingual('cfg-dlg-description', draft.description)}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_shortProfileDescription'))}</label><div class="input-wrap">${bilingual('cfg-dlg-short', draft.shortProfileDescription)}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_whitelist'))}</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist"><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_node'))}</label><div class="input-wrap"><select data-field="node">${nodeOptions(draft.node)}</select></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_masterLink'))}</label><div class="input-wrap"><input type="text" data-field="masterLink" spellcheck="false"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_json'))}</label><div class="input-wrap"><textarea class="config-json" data-field="json" spellcheck="false">{}</textarea></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_xhttpExtra'))}</label><div class="input-wrap"><textarea class="config-json" data-field="xhttpExtra" spellcheck="false">{}</textarea></div></div>
    ${fieldError('cfg-dialog-err')}
  </div>`;
}

function dialogPanel() {
  return `<div class="form-grid">
    <div class="form-row"><label>${escapeHtml(t('config_field_key'))}</label><div class="input-wrap"><input type="text" data-field="id" spellcheck="false"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_name'))}</label><div class="input-wrap"><input type="text" data-field="name"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_address'))}</label><div class="input-wrap"><input type="text" data-field="address" value="127.0.0.1" spellcheck="false"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_uri'))}</label><div class="input-wrap"><input type="text" data-field="uri" spellcheck="false"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_port'))}</label><div class="input-wrap"><input type="text" data-field="port" spellcheck="false"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_token'))}</label><div class="input-wrap">${passWrap('cfg-dlg-token', '')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_https'))}</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="https"><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_whitelist'))}</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist"><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_mode'))}</label><div class="input-wrap"><select data-field="mode"><option value="blacklist" selected>${escapeHtml(t('config_blacklist_mode'))}</option><option value="whitelist">${escapeHtml(t('config_whitelist_mode'))}</option></select></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_inbounds_list'))}</label><div class="input-wrap">${rowList('cfg-dlg-inbounds', [], 'int')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_nginx_auth'))}</label><div class="input-wrap" data-dialog-nested="nginx_auth"></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_inject_headers'))}</label><div class="input-wrap" data-dialog-nested="inject_headers"></div></div>
    ${fieldError('cfg-dialog-err')}
  </div>`;
}

function dialogValue(body: HTMLElement, field: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null {
  return body.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[data-field="${field}"]`);
}

function readDialogInbound(body: HTMLElement) {
  const json = parseJsonField(dialogValue(body, 'json')?.value, {});
  const extra = parseJsonField(dialogValue(body, 'xhttpExtra')?.value, {});
  return {
    id: dialogValue(body, 'id')?.value ?? '',
    flag: dialogValue(body, 'flag')?.value ?? '',
    name: [readText('cfg-dlg-name-0'), readText('cfg-dlg-name-1')],
    description: [readText('cfg-dlg-description-0'), readText('cfg-dlg-description-1')],
    shortProfileDescription: [readText('cfg-dlg-short-0'), readText('cfg-dlg-short-1')],
    whitelist: (dialogValue(body, 'whitelist') as HTMLInputElement | null)?.checked ?? false,
    node: dialogValue(body, 'node')?.value ?? '',
    masterLink: dialogValue(body, 'masterLink')?.value ?? '',
    json, xhttpExtra: extra,
  };
}

function readDialogPanel(body: HTMLElement): ConfigObject & { id: string } {
  const port = dialogValue(body, 'port')?.value ?? '';
  const result: ConfigObject & { id: string } = {
    id: dialogValue(body, 'id')?.value ?? '',
    name: dialogValue(body, 'name')?.value ?? '',
    address: dialogValue(body, 'address')?.value ?? '',
    uri: dialogValue(body, 'uri')?.value ?? '',
    port: INTEGER_RE.test(port) ? new ConfigNumber(port) : port,
    token: readText('cfg-dlg-token'),
    https: (dialogValue(body, 'https') as HTMLInputElement | null)?.checked ?? false,
    whitelist: (dialogValue(body, 'whitelist') as HTMLInputElement | null)?.checked ?? false,
    mode: dialogValue(body, 'mode')?.value || 'blacklist',
    inbounds_list: readRows('cfg-dlg-inbounds', 'int').map(raw => { const value = String(raw); return INTEGER_RE.test(value.trim()) ? new ConfigNumber(value.trim()) : value; }),
  };
  const nginx = dialogValue(body, 'nginx0');
  if (nginx) result.nginx_auth = [nginx.value, readText('cfg-dlg-nginx-password')];
  if (document.getElementById('cfg-dlg-headers')) result.inject_headers = Object.fromEntries(readRows('cfg-dlg-headers', 'pair').filter((row): row is string[] => Array.isArray(row)).map(row => [row[0] ?? '', row[1] ?? '']));
  return result;
}

import { sharedMessage } from '../../shared/messages';
import { withBusy } from '../../shared/busy';
import { escapeHtml, loadingHtml as renderLoading } from '../../shared/dom';
import type { AdminContext } from '../context';
import { createMainEditor } from './main';
import { createCardEditor } from './cards';
import { createConfigDialog } from './dialog';
import { destroyJsonEditors, refreshJsonEditors } from './editor-bridge';
import { editor } from './state';
import { clone, jsonEqual, isPlainObject, parseConfigJson, stringifyConfig } from './json';
import type { ConfigObject } from './json';
import { showConfigSection } from './controls';
import { validateAll, focusIssue } from './validation';

export { revealConfigPass, showConfigSection } from './controls';

let context: AdminContext;
let pageLanguage = 'en';
let cleanupCards: () => void = () => {};
let disposed = false;
const isDisposed = (): boolean => disposed;
let generation = 0;
const t = (key: string): string => context.t(key);
const loadingHtml = (): string => renderLoading(t);
const api = (method: string, path: string, body?: unknown) => context.api(method, path, body);
const toast = (message: string, kind: string): void => { context.toast(message, kind); };
const confirmDialog = (message: string, title: string) => context.confirm(message, title);
export function initConfig(env: AdminContext, language: string): void { context = env; pageLanguage = language; disposed = false; generation++; cleanupCards = initCards(); }
export function setConfigLanguage(language: string): void { pageLanguage = language; }
function required(id: string): HTMLElement {
  const result = document.getElementById(id);
  if (!result) throw new Error(sharedMessage('missing_element', { id }));
  return result;
}
function configButton(id: string): HTMLButtonElement {
  const result = required(id);
  if (!(result instanceof HTMLButtonElement)) throw new Error(sharedMessage('missing_element', { id }));
  return result;
}

const { renderMain } = createMainEditor(markDirty, t);
const { init: initCards, renderCards, toggleConfigCard, deleteConfigCard } = createCardEditor({
  markDirty,
  openNew: kind => { openNew(kind); },
  t,
  language: () => pageLanguage,
  confirm: confirmDialog,
});
const openNew = createConfigDialog({ markDirty, renderCards });

export { toggleConfigCard, deleteConfigCard };

function markDirty(): void {
  if (disposed || !editor.loaded) return;
  editor.dirty = !jsonEqual(editor.draft ?? undefined, editor.loaded);
  if (editor.nodeRows && new Set(editor.nodeRows.map(row => row[0])).size !== editor.nodeRows.length) editor.dirty = true;
  editor.headerRows.forEach(rows => { if (new Set(rows.map(row => row[0])).size !== rows.length) editor.dirty = true; });
  document.querySelectorAll<HTMLInputElement>('#tab-config .panel-card [data-field="id"]').forEach(input => {
    if (input.value.trim() !== input.closest<HTMLElement>('.panel-card')?.dataset['id']) editor.dirty = true;
  });
  const label = document.getElementById('configDirty');
  if (label) label.textContent = editor.dirty ? t('config_unsaved') : t('config_saved_state');
  const save = document.querySelector<HTMLButtonElement>('#configSave');
  if (save && !editor.saving) save.disabled = !editor.dirty || editor.conflict;
  if (editor.dirty) window.addEventListener('beforeunload', onBeforeUnload);
  else window.removeEventListener('beforeunload', onBeforeUnload);
}

function onBeforeUnload(event: BeforeUnloadEvent): void {
  if (!editor.dirty) return;
  event.preventDefault();
  Reflect.set(event, 'returnValue', '');
}

function showResult(message: string, kind = ''): void {
  const el = document.getElementById('configResult');
  if (!el) return;
  el.textContent = message || '';
  el.className = 'config-result' + (kind ? ' ' + kind : '');
  el.hidden = !message;
}

function safeServerMessage(message: string | null | undefined): string {
  // Schema validation messages can quote values, including whole objects with secrets.
  if ((message || '').startsWith('Schema validation error:')) return t('config_err_schema');
  return message || t('error');
}

function setRestartLine(keys: string[]): void {
  editor.restartLine = keys.length ? t('config_restart_saved').replace('{keys}', keys.join(', ')) : '';
  const el = document.getElementById('configRestartSaved');
  if (!el) return;
  el.textContent = editor.restartLine;
  el.hidden = !editor.restartLine;
}

function diffValues(): ConfigObject {
  const values: ConfigObject = {};
  if (!editor.loaded || !editor.draft) return values;
  const loaded = editor.loaded;
  const draft = editor.draft;
  const keys = new Set([...Object.keys(loaded), ...Object.keys(draft)]);
  keys.forEach(key => {
    if (key === '$schema') return;
    const had = Object.prototype.hasOwnProperty.call(loaded, key);
    const has = Object.prototype.hasOwnProperty.call(draft, key);
    if (!had && !has) return;
    if (had && !has) values[key] = null;
    else if (draft[key] !== undefined && !jsonEqual(draft[key], had ? loaded[key] : undefined)) values[key] = clone(draft[key]);
  });
  return values;
}

function adoptSaved(base: string | undefined, snapshot: ConfigObject) {
  editor.loaded = snapshot;
  if (base) editor.etag = base;
  editor.conflict = false;
  markDirty();
}

export async function saveConfig() {
  if (disposed || editor.saving || editor.loading || !editor.loaded || !editor.draft) return;
  const current = generation;
  const button = configButton('configSave');
  await withBusy(button, async () => {
    editor.saving = true;
    button.disabled = true;
    configButton('configReload').disabled = true;
    try {
      const issues = validateAll();
      if (issues.length) {
        focusIssue(issues[0]);
        return;
      }
      if (editor.conflict) {
        showConflict();
        return;
      }
      const values = diffValues();
      if (!Object.keys(values).length) {
        toast(t('config_unchanged'), 'info');
        return;
      }
      if (!editor.draft) return;
      const snapshot = clone(editor.draft);
      const body: ConfigObject = { base: editor.etag, values };
      Object.defineProperty(body, Symbol.for('api.serialize'), { value: () => stringifyConfig(body) });
      const res = await api('POST', '/api/config/set', body);
      if (disposed || current !== generation) return;
      if (!res) {
        showResult(t('network_err'), 'error');
        return;
      }
      if (res.status === 409) {
        editor.conflict = true;
        showResult(safeServerMessage(res.msg || t('config_conflict')), 'error');
        showConflict();
        return;
      }
      if (res.status !== 200 || res.success !== true) {
        showResult(safeServerMessage(res.msg), 'error');
        if ((res.rawMsg || '').includes('profiles')) showConfigSection('inbounds');
        else if ((res.rawMsg || '').includes('3xui')) showConfigSection('panels');
        return;
      }
      if (Object.hasOwn(values, 'api_token')) {
        delete snapshot['api_token'];
        // A replacement entered while this request was in flight is a new edit,
        // not the credential just saved; retain it for the next save.
        if (jsonEqual(editor.draft['api_token'], values['api_token'])) {
          delete editor.draft['api_token'];
          const token = document.getElementById('cfg-api_token');
          if (token instanceof HTMLInputElement) token.value = '';
        }
      }
      const result = isPlainObject(res.obj) ? res.obj : {};
      adoptSaved(typeof result['base'] === 'string' ? result['base'] : undefined, snapshot);
      if (Object.hasOwn(values, 'api_admin_ui_auth')) {
        window.removeEventListener('beforeunload', onBeforeUnload);
        window.location.href = location.pathname.replace(/\/+$/, '') + '/login';
        return;
      }
      showResult(res.rawMsg === 'Unchanged' ? t('config_unchanged') : t('config_saved_state'), 'ok');
      if (res.rawMsg === 'Unchanged') setRestartLine([]);
      else setRestartLine(Array.isArray(result['restart']) ? result['restart'].filter((value): value is string => typeof value === 'string') : []);
    } catch {
      if (!disposed && current === generation) showResult(t('network_err'), 'error');
    } finally {
      editor.saving = false;
      if (!disposed && current === generation) { configButton('configReload').disabled = false; markDirty(); }
    }
  });
  if (!isDisposed() && current === generation) markDirty();
}

function showConflict(): void {
  const el = document.getElementById('configResult');
  if (!el || el.querySelector('[data-discard]')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn-tiny';
  button.dataset['discard'] = '1';
  button.textContent = t('config_discard_reload');
  button.addEventListener('click', () => { void loadConfig(true); });
  el.appendChild(button);
}

export async function reloadConfig() {
  if (disposed || editor.saving || editor.loading) return;
  if (editor.dirty) {
    const ok = await confirmDialog(t('config_leave_confirm'), t('confirm'));
    if (!ok || isDisposed()) return;
  }
  await loadConfig(true);
}

async function loadConfig(force: boolean): Promise<void> {
  if (disposed || editor.loading || editor.saving || (editor.fetched && !force)) return;
  const current = generation;
  editor.loading = true;
  const load = required('configLoad');
  const root = required('configEditor');
  load.hidden = false;
  load.innerHTML = loadingHtml();
  root.hidden = true;
  let res;
  try { res = await api('GET', '/api/config/get'); }
  catch { res = null; }
  finally { editor.loading = false; }
  if (isDisposed() || current !== generation) return;
  if (!res || !res.success || !isPlainObject(res.obj) || !res.etag) {
    load.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg ? safeServerMessage(res.msg) : t('config_load_failed'))}</div><button type="button" class="btn-secondary" id="configRetry">${escapeHtml(t('config_retry'))}</button>`;
    required('configRetry').addEventListener('click', () => { void loadConfig(true); });
    return;
  }
  const parsed = res.rawJson ? parseConfigJson(res.rawJson) : null;
  const configDocument = parsed && isPlainObject(parsed) ? parsed['obj'] : res.obj;
  if (!isPlainObject(configDocument)) { load.textContent = t('config_load_failed'); return; }
  editor.loaded = clone(configDocument);
  editor.draft = clone(configDocument);
  editor.nodeRows = null;
  editor.headerRows.clear();
  editor.jsonInputs.clear();
  destroyJsonEditors(document.getElementById('configEditor'));
  required('config-pane-inbounds').innerHTML = '';
  required('config-pane-panels').innerHTML = '';
  editor.etag = res.etag || '';
  editor.fetched = true;
  editor.conflict = false;
  editor.open.inbounds.clear();
  editor.open.panels.clear();
  load.hidden = true;
  root.hidden = false;
  renderMain();
  renderCards('inbounds');
  renderCards('panels');
  showResult('');
  setRestartLine([]);
  markDirty();
}

export function configOnTab() {
  if (disposed) return;
  const active = document.getElementById('tab-config')?.classList.contains('active');
  if (active) refreshJsonEditors(document.getElementById('tab-config'));
  if (!active || editor.fetched) return;
  void loadConfig(false);
}

configOnTab.leave = async function leave() {
  if (disposed || editor.saving || editor.loading) return false;
  if (!editor.dirty) { editor.fetched = false; return true; }
  const ok = await confirmDialog(t('config_leave_confirm'), t('confirm'));
  if (!ok || isDisposed()) return false;
  editor.draft = clone(editor.loaded);
  editor.dirty = false;
  editor.fetched = false;
  editor.open.inbounds.clear();
  editor.open.panels.clear();
  window.removeEventListener('beforeunload', onBeforeUnload);
  return true;
};

export function configDispose() {
  disposed = true;
  generation++;
  window.removeEventListener('beforeunload', onBeforeUnload);
  cleanupCards();
  openNew.dispose();
  const root = document.getElementById('configEditor');
  if (root) destroyJsonEditors(root);
}

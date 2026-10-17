import { destroyJsonEditors, refreshJsonEditors } from './json-editor.js';
import { editor } from './config/config-state.js';
import { clone, jsonEqual, isPlainObject, parseConfigJson, stringifyConfig } from './config/config-json.js';
import { showConfigSection } from './config/config-controls.js';
import { validateAll, focusIssue } from './config/config-validation.js';
import { createMainEditor } from './config/config-main.js';
import { createCardEditor } from './config/config-cards.js';
import { createConfigDialog } from './config/config-dialog.js';

export { revealConfigPass, showConfigSection } from './config/config-controls.js';

const { renderMain } = createMainEditor(markDirty);
const { renderCards, toggleConfigCard, deleteConfigCard } = createCardEditor({
  markDirty,
  openNew: kind => openNew(kind),
});
const openNew = createConfigDialog({ markDirty, renderCards });

export { toggleConfigCard, deleteConfigCard };

function markDirty() {
  if (!editor.loaded) return;
  editor.dirty = !jsonEqual(editor.draft, editor.loaded);
  if (editor.nodeRows && new Set(editor.nodeRows.map(row => row[0])).size !== editor.nodeRows.length) editor.dirty = true;
  editor.headerRows.forEach(rows => { if (new Set(rows.map(row => row[0])).size !== rows.length) editor.dirty = true; });
  document.querySelectorAll('#tab-config .panel-card [data-field="id"]').forEach(input => {
    if (input.value.trim() !== input.closest('.panel-card').dataset.id) editor.dirty = true;
  });
  const label = document.getElementById('configDirty');
  if (label) label.textContent = editor.dirty ? t('config_unsaved') : t('config_saved_state');
  const save = document.getElementById('configSave');
  if (save && !editor.saving) save.disabled = !editor.dirty || editor.conflict;
  if (editor.dirty) window.addEventListener('beforeunload', onBeforeUnload);
  else window.removeEventListener('beforeunload', onBeforeUnload);
}

function onBeforeUnload(event) {
  if (!editor.dirty) return;
  event.preventDefault();
  event.returnValue = '';
}

function showResult(message, kind) {
  const el = document.getElementById('configResult');
  if (!el) return;
  el.textContent = message || '';
  el.className = 'config-result' + (kind ? ' ' + kind : '');
  el.hidden = !message;
}

function safeServerMessage(message) {
  // Schema validation messages can quote values, including whole objects with secrets.
  if (String(message || '').startsWith('Schema validation error:')) return t('config_err_schema');
  return message || t('error');
}

function setRestartLine(keys) {
  editor.restartLine = keys && keys.length ? t('config_restart_saved').replace('{keys}', keys.join(', ')) : '';
  const el = document.getElementById('configRestartSaved');
  if (!el) return;
  el.textContent = editor.restartLine;
  el.hidden = !editor.restartLine;
}

function diffValues() {
  const values = {};
  const keys = new Set([...Object.keys(editor.loaded), ...Object.keys(editor.draft)]);
  keys.forEach(key => {
    if (key === '$schema') return;
    const had = Object.prototype.hasOwnProperty.call(editor.loaded, key);
    const has = Object.prototype.hasOwnProperty.call(editor.draft, key);
    if (!had && !has) return;
    if (had && !has) values[key] = null;
    else if (!jsonEqual(editor.draft[key], had ? editor.loaded[key] : undefined)) values[key] = clone(editor.draft[key]);
  });
  return values;
}

function adoptSaved(base, snapshot) {
  editor.loaded = snapshot;
  if (base) editor.etag = base;
  editor.conflict = false;
  markDirty();
}

export async function saveConfig() {
  if (editor.saving || editor.loading || !editor.loaded) return;
  const button = document.getElementById('configSave');
  await withBusy(button, async () => {
    editor.saving = true;
    button.disabled = true;
    document.getElementById('configReload').disabled = true;
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
      const snapshot = clone(editor.draft);
      const body = { base: editor.etag, values };
      body[Symbol.for('api.serialize')] = () => stringifyConfig(body);
      const res = await api('POST', '/api/config/set', body);
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
        if (String(res.msg || '').includes('profiles')) showConfigSection('inbounds');
        else if (String(res.msg || '').includes('3xui')) showConfigSection('panels');
        return;
      }
      adoptSaved(res.obj?.base, snapshot);
      showResult(res.msg === 'Unchanged' ? t('config_unchanged') : t('config_saved_state'), 'ok');
      if (res.msg === 'Unchanged') setRestartLine([]);
      else setRestartLine(Array.isArray(res.obj?.restart) ? res.obj.restart : []);
    } catch {
      showResult(t('network_err'), 'error');
    } finally {
      editor.saving = false;
      document.getElementById('configReload').disabled = false;
      markDirty();
    }
  });
  markDirty();
}

function showConflict() {
  const el = document.getElementById('configResult');
  if (!el || el.querySelector('[data-discard]')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn-tiny';
  button.dataset.discard = '1';
  button.textContent = t('config_discard_reload');
  button.onclick = () => loadConfig(true);
  el.appendChild(button);
}

export async function reloadConfig() {
  if (editor.saving || editor.loading) return;
  if (editor.dirty) {
    const ok = await confirmDialog(t('config_leave_confirm'), t('confirm'));
    if (!ok) return;
  }
  await loadConfig(true);
}

async function loadConfig(force) {
  if (editor.loading || editor.saving || (editor.fetched && !force)) return;
  editor.loading = true;
  const load = document.getElementById('configLoad');
  const root = document.getElementById('configEditor');
  load.hidden = false;
  load.innerHTML = loadingHtml();
  root.hidden = true;
  let res;
  try { res = await api('GET', '/api/config/get'); }
  catch { res = null; }
  finally { editor.loading = false; }
  if (!res || !res.success || !isPlainObject(res.obj) || !res.etag) {
    load.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg ? safeServerMessage(res.msg) : t('config_load_failed'))}</div><button type="button" class="btn-secondary" id="configRetry">${escapeHtml(t('config_retry'))}</button>`;
    document.getElementById('configRetry').onclick = () => loadConfig(true);
    return;
  }
  const configDocument = res.rawJson ? parseConfigJson(res.rawJson).obj : res.obj;
  editor.loaded = clone(configDocument);
  editor.draft = clone(configDocument);
  editor.nodeRows = null;
  editor.headerRows.clear();
  editor.jsonInputs.clear();
  destroyJsonEditors(document.getElementById('configEditor'));
  document.getElementById('config-pane-inbounds').innerHTML = '';
  document.getElementById('config-pane-panels').innerHTML = '';
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
  const active = document.getElementById('tab-config')?.classList.contains('active');
  if (active) refreshJsonEditors(document.getElementById('tab-config'));
  if (!active || editor.fetched) return;
  loadConfig(false);
}

configOnTab.leave = async function leave() {
  if (editor.saving || editor.loading) return false;
  if (!editor.dirty) { editor.fetched = false; return true; }
  const ok = await confirmDialog(t('config_leave_confirm'), t('confirm'));
  if (!ok) return false;
  editor.draft = clone(editor.loaded);
  editor.dirty = false;
  editor.fetched = false;
  editor.open.inbounds.clear();
  editor.open.panels.clear();
  window.removeEventListener('beforeunload', onBeforeUnload);
  return true;
};

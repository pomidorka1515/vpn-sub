import { mountJsonEditor, refreshJsonEditors } from '../json-editor.js';
import { editor } from './config-state.js';
import { isPlainObject, parseConfigJson, stringifyConfig } from './config-json.js';

export function attr(value) {
  return escapeHtml(value).replace(/`/g, '&#96;');
}

export function restartIcon() {
  return `<span class="config-restart-wrap"><button type="button" class="config-restart-icon" aria-label="${attr(t('config_restart_tip'))}">!</button><span class="config-restart-tooltip" role="tooltip">${escapeHtml(t('config_restart_tip'))}</span></span>`;
}

export function fieldError(id) {
  return `<div class="config-error" id="${id}" hidden></div>`;
}

export function passWrap(id, value) {
  return `<div class="config-pass">
    <input type="password" id="${id}" autocomplete="new-password" spellcheck="false" value="${attr(value)}">
    <button type="button" class="pass-toggle" onclick="revealConfigPass('${id}', this)" data-t-aria="toggle_password" aria-label="${attr(t('toggle_password'))}"></button>
  </div>`;
}

export function setError(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || '';
}

export function revealConfigPass(id, btn) {
  togglePass(id, btn);
}

export function readText(id) {
  return document.getElementById(id)?.value ?? '';
}

export function enhanceJsonInputs(root) {
  root.querySelectorAll('textarea.config-json').forEach(textarea => mountJsonEditor(textarea, {
    parse: parseConfigJson,
    format: text => stringifyConfig(parseConfigJson(text), true),
    label: textarea.dataset.field || 'json_template',
  }));
}

export function rowList(id, rows, kind) {
  const body = rows.map((row, index) => {
    if (kind === 'pair') {
      return `<div class="config-rowline">
        <input type="text" data-row="${index}" data-side="key" value="${attr(row[0])}" spellcheck="false">
        <input type="text" data-row="${index}" data-side="value" value="${attr(row[1])}" spellcheck="false">
        <button type="button" class="btn-tiny danger" data-remove="${index}">${escapeHtml(t('config_remove'))}</button>
      </div>`;
    }
    if (kind === 'int') {
      return `<div class="config-rowline">
        <input type="text" inputmode="numeric" data-row="${index}" value="${attr(row)}" spellcheck="false">
        <button type="button" class="btn-tiny danger" data-remove="${index}">${escapeHtml(t('config_remove'))}</button>
      </div>`;
    }
    return `<div class="config-rowline">
      <input type="text" data-row="${index}" value="${attr(row)}" spellcheck="false">
      <button type="button" class="btn-tiny danger" data-remove="${index}">${escapeHtml(t('config_remove'))}</button>
    </div>`;
  }).join('');
  return `<div class="config-rows" id="${id}">${body}</div>
    <button type="button" class="btn-tiny" data-add="${id}">${escapeHtml(t('config_add'))}</button>`;
}

export function readRows(id, kind) {
  const root = document.getElementById(id);
  if (!root) return [];
  if (kind === 'pair') {
    const map = new Map();
    root.querySelectorAll('input').forEach(input => {
      const index = input.dataset.row;
      const current = map.get(index) || ['', ''];
      current[input.dataset.side === 'value' ? 1 : 0] = input.value;
      map.set(index, current);
    });
    return [...map.values()];
  }
  return [...root.querySelectorAll('input')].map(input => input.value);
}

export function bindRows(id, kind, apply) {
  const root = document.getElementById(id);
  if (!root) return;
  const host = root.parentElement;
  host.querySelector(`[data-add="${id}"]`)?.addEventListener('click', () => {
    const rows = readRows(id, kind);
    rows.push(kind === 'pair' ? ['', ''] : '');
    apply(rows);
  });
  root.addEventListener('click', event => {
    const btn = event.target.closest('[data-remove]');
    if (!btn) return;
    const rows = readRows(id, kind);
    rows.splice(Number(btn.dataset.remove), 1);
    apply(rows);
  });
  root.addEventListener('input', () => apply(readRows(id, kind), true));
}

export function bilingual(prefix, pair) {
  const values = Array.isArray(pair) ? pair : ['', ''];
  return `<div class="config-pair">
    <input type="text" id="${prefix}-0" value="${attr(values[0] ?? '')}" data-t-ph="config_en" placeholder="${attr(t('config_en'))}">
    <input type="text" id="${prefix}-1" value="${attr(values[1] ?? '')}" data-t-ph="config_ru" placeholder="${attr(t('config_ru'))}">
  </div>`;
}

export function nodeOptions(selected) {
  const keys = isPlainObject(editor.draft.nodes) ? Object.keys(editor.draft.nodes) : [];
  const options = keys.map(key => `<option value="${attr(key)}"${key === selected ? ' selected' : ''}>${escapeHtml(key)}</option>`);
  if (selected && !keys.includes(selected)) options.unshift(`<option value="${attr(selected)}" selected>${escapeHtml(selected)}</option>`);
  if (!selected) options.unshift(`<option value="" selected></option>`);
  return options.join('');
}

export function cssId(id) {
  return Array.from(id, char => char.codePointAt(0).toString(16)).join('-');
}

export function cardRoot(kind, id) {
  return [...document.querySelectorAll('#tab-config .panel-card')].find(card => card.dataset.kind === kind && card.getAttribute('data-id') === id);
}

export function showConfigSection(name) {
  document.querySelectorAll('.config-tab').forEach(el => el.classList.toggle('active', el.dataset.section === name));
  document.querySelectorAll('.config-pane').forEach(el => el.classList.toggle('active', el.id === 'config-pane-' + name));
  refreshJsonEditors(document.getElementById('config-pane-' + name));
}

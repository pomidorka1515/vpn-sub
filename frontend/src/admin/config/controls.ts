import { escapeHtml } from '../../shared/dom';
import type { Translate } from '../../shared/formatting';
import { togglePass } from '../../shared/passwords';
import { mountJsonEditor, refreshJsonEditors } from './editor-bridge';
import { editor } from './state';
import { isPlainObject, parseConfigJson, stringifyConfig } from './json';

let t: Translate = key => key;
export function initConfigControls(translate: Translate): () => void {
  t = translate;
  return () => { t = key => key; };
}

export function attr(value: unknown): string {
  return escapeHtml(value).replace(/`/g, '&#96;');
}

export function restartIcon() {
  return `<span class="config-restart-wrap"><button type="button" class="config-restart-icon" aria-label="${attr(t('config_restart_tip'))}">!</button><span class="config-restart-tooltip" role="tooltip">${escapeHtml(t('config_restart_tip'))}</span></span>`;
}

export function fieldError(id: string): string {
  return `<div class="config-error" id="${id}" hidden></div>`;
}

export function passWrap(id: string, value: unknown): string {
  return `<div class="config-pass">
    <input type="password" id="${id}" autocomplete="new-password" spellcheck="false" value="${attr(value)}">
    <button type="button" class="pass-toggle" data-config-pass="${id}" data-t-aria="toggle_password" aria-label="${attr(t('toggle_password'))}"></button>
  </div>`;
}

export function setError(id: string, message: string): void {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || '';
}

export function revealConfigPass(id: string, btn: HTMLElement): void {
  togglePass(document, id, btn);
}

export function readText(id: string): string {
  const input = document.getElementById(id);
  return input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement || input instanceof HTMLSelectElement ? input.value : '';
}

export function enhanceJsonInputs(root: HTMLElement): void {
  root.querySelectorAll<HTMLTextAreaElement>('textarea.config-json').forEach(textarea => { void mountJsonEditor(textarea, {
    parse: parseConfigJson,
    format: text => stringifyConfig(parseConfigJson(text), true),
    label: t('config_field_' + (textarea.dataset.field || 'json_template')),
  }); });
}

export function rowList(id: string, rows: Array<string | string[]>, kind: string): string {
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

export function readRows(id: string, kind: string): Array<string | string[]> {
  const root = document.getElementById(id);
  if (!root) return [];
  if (kind === 'pair') {
    const map = new Map<string | undefined, string[]>();
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

export function bindRows(id: string, kind: string, apply: (rows: Array<string | string[]>, live?: boolean) => void): void {
  const root = document.getElementById(id);
  if (!root) return;
  const host = root.parentElement;
  host?.querySelector(`[data-add="${id}"]`)?.addEventListener('click', () => {
    const rows = readRows(id, kind);
    rows.push(kind === 'pair' ? ['', ''] : '');
    apply(rows);
  });
  root.addEventListener('click', event => {
    const btn = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-remove]') : null;
    if (!btn) return;
    const rows = readRows(id, kind);
    rows.splice(Number(btn.dataset.remove), 1);
    apply(rows);
  });
  root.addEventListener('input', () => apply(readRows(id, kind), true));
}

export function bilingual(prefix: string, pair: unknown): string {
  const values = Array.isArray(pair) ? pair : ['', ''];
  return `<div class="config-pair">
    <input type="text" id="${prefix}-0" value="${attr(values[0] ?? '')}" data-t-ph="config_en" placeholder="${attr(t('config_en'))}">
    <input type="text" id="${prefix}-1" value="${attr(values[1] ?? '')}" data-t-ph="config_ru" placeholder="${attr(t('config_ru'))}">
  </div>`;
}

export function nodeOptions(selected: string): string {
  const keys = isPlainObject(editor.draft?.nodes) ? Object.keys(editor.draft.nodes) : [];
  const options = keys.map(key => `<option value="${attr(key)}"${key === selected ? ' selected' : ''}>${escapeHtml(key)}</option>`);
  if (selected && !keys.includes(selected)) options.unshift(`<option value="${attr(selected)}" selected>${escapeHtml(selected)}</option>`);
  if (!selected) options.unshift(`<option value="" selected></option>`);
  return options.join('');
}

export function cssId(id: string): string {
  return Array.from(id, char => (char.codePointAt(0) ?? 0).toString(16)).join('-');
}

export function cardRoot(kind: string, id: string): HTMLElement | undefined {
  return [...document.querySelectorAll<HTMLElement>('#tab-config .panel-card')].find(card => card.dataset.kind === kind && card.getAttribute('data-id') === id);
}

export function showConfigSection(name: string): void {
  document.querySelectorAll<HTMLElement>('.config-tab').forEach(el => el.classList.toggle('active', el.dataset.section === name));
  document.querySelectorAll('.config-pane').forEach(el => el.classList.toggle('active', el.id === 'config-pane-' + name));
  refreshJsonEditors(document.getElementById('config-pane-' + name));
}

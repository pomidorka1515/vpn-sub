import { displayValue, escapeHtml } from '../../shared/dom';
import type { Translate } from '../../shared/formatting';
import { initPassToggles } from '../../shared/passwords';
import { destroyJsonEditors, refreshJsonEditors } from './editor-bridge';
import { editor, INTEGER_RE } from './state';
import { ConfigNumber, isPlainObject, jsonText, parseJsonField } from './json';
import type { ConfigObject, ConfigValue } from './json';
import { attr, restartIcon, fieldError, passWrap, readText, enhanceJsonInputs, rowList, readRows, bindRows, bilingual, nodeOptions, cssId, cardRoot } from './controls';
import { validateCard, validateAll } from './validation';

type Kind = 'inbounds' | 'panels';
type Pair = [string, string];
export function createCardEditor({ markDirty, openNew, t, language, confirm }: { markDirty: () => void; openNew: (kind: Kind) => void; t: Translate; language: () => string; confirm: (message: string, title: string) => Promise<boolean | string | null> }) {
function mapOf(kind: Kind): ConfigObject {
  const value = kind === 'inbounds' ? editor.draft?.['profiles'] : editor.draft?.['3xui'];
  return isPlainObject(value) ? value : {};
}
function cardData(kind: Kind, id: string): ConfigObject { const value = mapOf(kind)[id]; return isPlainObject(value) ? value : {}; }
function pairs(rows: Array<string | string[]>): Pair[] { return rows.filter((row): row is string[] => Array.isArray(row)).map(row => [row[0] ?? '', row[1] ?? '']); }

function profileJsonKey(id: string, field: string): string {
  return JSON.stringify([id, field]);
}

function profileJsonText(id: string, field: string, value: ConfigValue | undefined): string {
  return editor.jsonInputs.get(profileJsonKey(id, field)) ?? jsonText(value);
}

function profileHeader(id: string, profile: ConfigObject): string {
  const lang = language() === 'ru' ? 1 : 0;
  const name = Array.isArray(profile['name']) ? (profile['name'][lang] || profile['name'][0] || '') : '';
  return `${escapeHtml(profile['flag'] || '')} <span class="panel-card-name">${escapeHtml(id)}</span> <span>${escapeHtml(name)}</span>`;
}

function panelHeader(id: string, panel: ConfigObject): string {
  return `<span class="panel-card-name">${escapeHtml(id)}</span> <span>${escapeHtml(panel['name'] || '')}</span> <span class="badge config-panel-mode" data-mode="${attr(panel['mode'] || '')}">${escapeHtml(t(panel['mode'] === 'whitelist' ? 'config_whitelist_mode' : panel['mode'] === 'blacklist' ? 'config_blacklist_mode' : 'unknown'))}</span>`;
}

function cardShell(kind: Kind, id: string, titleHtml: string, body: string, open: boolean): string {
  const safe = attr(id);
  return `<div class="panel-card${open ? ' open' : ''}" data-kind="${kind}" data-id="${safe}">
    <div class="panel-card-header">
      <div class="panel-card-title">${titleHtml}</div>
      <div class="panel-card-meta">
        ${kind === 'panels' ? restartIcon() : ''}
        <button type="button" class="btn-tiny danger config-card-delete">${escapeHtml(t('delete'))}</button>
        <button type="button" class="config-card-toggle" aria-expanded="${String(open)}" aria-label="${safe}"><svg class="panel-card-arrow" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4,6 8,10 12,6"/></svg></button>
      </div>
    </div>
    <div class="panel-card-body">${body}</div>
  </div>`;
}

function inboundBody(id: string, profile: ConfigObject): string {
  const p = profile;
  return `<div class="form-grid">
    <div class="form-row"><label>${escapeHtml(t('config_field_id'))}</label><div class="input-wrap"><input type="text" data-field="id" value="${attr(id)}" spellcheck="false">${fieldError('cfg-in-err-' + cssId(id) + '-id')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_flag'))}</label><div class="input-wrap"><input type="text" data-field="flag" value="${attr(p['flag'] ?? '')}">${fieldError('cfg-in-err-' + cssId(id) + '-flag')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_name'))}</label><div class="input-wrap">${bilingual('cfg-in-' + cssId(id) + '-name', p['name'])}${fieldError('cfg-in-err-' + cssId(id) + '-name')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_description'))}</label><div class="input-wrap">${bilingual('cfg-in-' + cssId(id) + '-description', p['description'])}${fieldError('cfg-in-err-' + cssId(id) + '-description')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_shortProfileDescription'))}</label><div class="input-wrap">${bilingual('cfg-in-' + cssId(id) + '-short', p['shortProfileDescription'])}${fieldError('cfg-in-err-' + cssId(id) + '-shortProfileDescription')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_whitelist'))}</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist" ${p['whitelist'] ? 'checked' : ''}><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_node'))}</label><div class="input-wrap"><select data-field="node" class="cfg-node-select">${nodeOptions(typeof p['node'] === 'string' ? p['node'] : '')}</select>${fieldError('cfg-in-err-' + cssId(id) + '-node')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_masterLink'))}</label><div class="input-wrap"><input type="text" data-field="masterLink" value="${attr(p['masterLink'] ?? '')}" spellcheck="false" aria-describedby="cfg-in-link-help-${cssId(id)}"><div class="config-sub" id="cfg-in-link-help-${cssId(id)}">${escapeHtml(t('config_master_link_help'))}</div>${fieldError('cfg-in-err-' + cssId(id) + '-masterLink')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_json'))}</label><div class="input-wrap"><textarea class="config-json" data-field="json" spellcheck="false">${escapeHtml(profileJsonText(id, 'json', p['json']))}</textarea>${fieldError('cfg-in-err-' + cssId(id) + '-json')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_xhttpExtra'))}</label><div class="input-wrap"><textarea class="config-json" data-field="xhttpExtra" spellcheck="false">${escapeHtml(profileJsonText(id, 'xhttpExtra', p['xhttpExtra']))}</textarea>${fieldError('cfg-in-err-' + cssId(id) + '-xhttpExtra')}</div></div>
  </div>`;
}

function panelBody(id: string, panel: ConfigObject): string {
  const p = panel;
  const headers = isPlainObject(p['inject_headers']) ? editor.headerRows.get(id) || Object.entries(p['inject_headers']).map(([key, value]) => [key, displayValue(value)]) : null;
  return `<div class="form-grid">
    <div class="form-row"><label>${escapeHtml(t('config_field_key'))}</label><div class="input-wrap"><input type="text" data-field="id" value="${attr(id)}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-id')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_name'))}</label><div class="input-wrap"><input type="text" data-field="name" value="${attr(p['name'] ?? '')}">${fieldError('cfg-pn-err-' + cssId(id) + '-name')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_address'))}</label><div class="input-wrap"><input type="text" data-field="address" value="${attr(p['address'] ?? '')}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-address')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_uri'))}</label><div class="input-wrap"><input type="text" data-field="uri" value="${attr(p['uri'] ?? '')}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-uri')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_port'))}</label><div class="input-wrap"><input type="text" data-field="port" value="${attr(p['port'] ?? '')}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-port')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_token'))}</label><div class="input-wrap">${passWrap('cfg-pn-token-' + cssId(id), p['token'] ?? '')}${fieldError('cfg-pn-err-' + cssId(id) + '-token')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_https'))}</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="https" ${p['https'] ? 'checked' : ''}><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_whitelist'))}</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist" ${p['whitelist'] ? 'checked' : ''}><div class="check-track"><div class="check-thumb"></div></div></label>${fieldError('cfg-pn-err-' + cssId(id) + '-whitelist')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_mode'))}</label><div class="input-wrap"><select data-field="mode" aria-describedby="cfg-pn-mode-help-${cssId(id)}"><option value="blacklist"${p['mode'] === 'blacklist' ? ' selected' : ''}>${escapeHtml(t('config_blacklist_mode'))}</option><option value="whitelist"${p['mode'] === 'whitelist' ? ' selected' : ''}>${escapeHtml(t('config_whitelist_mode'))}</option></select><div class="config-sub" id="cfg-pn-mode-help-${cssId(id)}">${escapeHtml(t('config_panel_mode_help'))}</div>${fieldError('cfg-pn-err-' + cssId(id) + '-mode')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_inbounds_list'))}</label><div class="input-wrap" id="cfg-pn-inbounds-${cssId(id)}">${rowList('cfg-pn-rows-' + cssId(id), (Array.isArray(p['inbounds_list']) ? p['inbounds_list'] : []).map(String), 'int')}${fieldError('cfg-pn-err-' + cssId(id) + '-inbounds_list')}</div></div>
    <div class="form-row"><label>${escapeHtml(t('config_field_inject_headers'))}</label><div class="input-wrap">${headers ? `${rowList('cfg-pn-headers-' + cssId(id), headers, 'pair')}<button type="button" class="btn-tiny danger" data-nested-remove="inject_headers">${escapeHtml(t('config_remove_headers'))}</button>` : `<div class="config-absent">${escapeHtml(t('config_not_set'))} <button type="button" class="btn-tiny" data-nested-add="inject_headers">${escapeHtml(t('config_add'))}</button></div>`}${fieldError('cfg-pn-err-' + cssId(id) + '-inject_headers')}</div></div>
  </div>`;
}

  function renderCards(kind: Kind): void {
    const pane = document.getElementById('config-pane-' + kind);
    if (!pane) return;
    destroyJsonEditors(pane);
    // Pending invalid renames and duplicate header rows belong to the draft too.
    const pendingIds = new Map([...pane.querySelectorAll<HTMLElement>('.panel-card')].map(card => [card.dataset['id'] ?? '', card.querySelector<HTMLInputElement>('[data-field="id"]')?.value]));
    const map = mapOf(kind);
    const subtitle = kind === 'inbounds' ? t('config_inbounds_sub') : t('config_panels_sub');
    const cards = Object.entries(isPlainObject(map) ? map : {}).map(([id, value]) => {
      const open = editor.open[kind].has(id);
      const item = isPlainObject(value) ? value : {};
      const title = kind === 'inbounds' ? profileHeader(id, item) : panelHeader(id, item);
      const body = kind === 'inbounds' ? inboundBody(id, item) : panelBody(id, item);
      return cardShell(kind, id, title, body, open);
    }).join('');
    pane.innerHTML = `<div class="config-subtitle"><span>${escapeHtml(subtitle)}</span></div>
      <div class="toolbar"><button type="button" class="btn btn-secondary" id="cfg-new-${kind}">${escapeHtml(t('config_new'))}</button></div>
      <div class="panel-cards">${cards}</div>`;
    document.getElementById('cfg-new-' + kind)?.addEventListener('click', () => { openNew(kind); });
    Object.keys(isPlainObject(map) ? map : {}).forEach(id => { bindCard(kind, id); });
    pendingIds.forEach((raw, id) => {
      const input = cardRoot(kind, id)?.querySelector<HTMLInputElement>('[data-field="id"]');
      if (input && raw !== undefined) input.value = raw;
    });
    initPassToggles(document);
    Object.keys(isPlainObject(map) ? map : {}).forEach(id => { validateCard(kind, id); });
  }

  function bindCard(kind: Kind, id: string): void {
    const card = cardRoot(kind, id);
    if (!card) return;
    const commit = () => { commitCard(kind, id); };
    card.querySelector('.config-card-delete')?.addEventListener('click', event => { void deleteConfigCard(event, kind, id); });
    card.querySelector('.config-card-toggle')?.addEventListener('click', () => { toggleConfigCard(kind, id); });
    card.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('input, textarea, select').forEach(input => {
      if (input.dataset['field'] === 'id') {
        input.addEventListener('input', () => { validateCard(kind, id); markDirty(); });
        input.addEventListener('blur', () => { renameCard(kind, id, input.value); });
        return;
      }
      input.addEventListener('input', commit);
      input.addEventListener('change', commit);
    });
    if (kind === 'inbounds' && card.classList.contains('open')) enhanceJsonInputs(card);
    if (kind === 'panels') {
      bindRows('cfg-pn-rows-' + cssId(id), 'int', (rows, live) => {
        cardData('panels', id)['inbounds_list'] = rows.map(row => { const raw = String(row); return INTEGER_RE.test(raw.trim()) ? new ConfigNumber(raw.trim()) : raw; });
        if (!live) renderCards('panels');
        else validateCard('panels', id);
        markDirty();
      });
      bindRows('cfg-pn-headers-' + cssId(id), 'pair', (rows, live) => {
        const headerRows = pairs(rows);
        editor.headerRows.set(id, headerRows);
        const headers: ConfigObject = {};
        Object.setPrototypeOf(headers, null);
        headerRows.forEach(([key, value]) => { headers[key] = value; });
        cardData('panels', id)['inject_headers'] = headers;
        if (!live) renderCards('panels');
        else validateCard('panels', id);
        markDirty();
      });
      card.querySelectorAll<HTMLElement>('[data-nested-add]').forEach(button => { button.addEventListener('click', () => {
        cardData('panels', id)['inject_headers'] = {};
        editor.open.panels.add(id);
        renderCards('panels');
        markDirty();
      }); });
      card.querySelectorAll<HTMLElement>('[data-nested-remove]').forEach(button => { button.addEventListener('click', () => {
        const which = button.dataset['nestedRemove'];
        if (!which) return;
        if (which === 'inject_headers') editor.headerRows.delete(id);
        Reflect.deleteProperty(cardData('panels', id), which);
        editor.open.panels.add(id);
        renderCards('panels');
        markDirty();
      }); });
    }
  }

  function readCard(kind: Kind, id: string): ConfigObject | null {
    const card = cardRoot(kind, id);
    if (!card) return null;
    const value = (input: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null => card.querySelector(`[data-field="${input}"]`);
    if (kind === 'inbounds') {
      const current = cardData('inbounds', id);
      const next: ConfigObject = { ...current };
      next['flag'] = value('flag')?.value ?? '';
      const whitelist = value('whitelist');
      next['whitelist'] = whitelist instanceof HTMLInputElement && whitelist.checked;
      next['node'] = value('node')?.value ?? '';
      next['masterLink'] = value('masterLink')?.value ?? '';
      next['name'] = [readText('cfg-in-' + cssId(id) + '-name-0'), readText('cfg-in-' + cssId(id) + '-name-1')];
      next['description'] = [readText('cfg-in-' + cssId(id) + '-description-0'), readText('cfg-in-' + cssId(id) + '-description-1')];
      next['shortProfileDescription'] = [readText('cfg-in-' + cssId(id) + '-short-0'), readText('cfg-in-' + cssId(id) + '-short-1')];
      for (const field of ['json', 'xhttpExtra']) {
        const raw = value(field)?.value;
        if (raw !== undefined) editor.jsonInputs.set(profileJsonKey(id, field), raw);
        next[field] = parseJsonField(raw, current[field] ?? null);
      }
      return next;
    }
    const current = cardData('panels', id);
    const next: ConfigObject = { ...current };
    next['name'] = value('name')?.value ?? '';
    next['address'] = value('address')?.value ?? '';
    next['uri'] = value('uri')?.value ?? '';
    const port = value('port')?.value ?? '';
    next['port'] = port === displayValue(current['port']) ? current['port'] ?? null : INTEGER_RE.test(port) ? new ConfigNumber(port) : port;
    const token = document.getElementById('cfg-pn-token-' + cssId(id));
    if (token instanceof HTMLInputElement) next['token'] = token.value;
    const https = value('https');
    const whitelist = value('whitelist');
    next['https'] = https instanceof HTMLInputElement && https.checked;
    next['whitelist'] = whitelist instanceof HTMLInputElement && whitelist.checked;
    next['mode'] = value('mode')?.value || 'blacklist';
    next['inbounds_list'] = readRows('cfg-pn-rows-' + cssId(id), 'int').map(row => { const raw = String(row); return INTEGER_RE.test(raw.trim()) ? new ConfigNumber(raw.trim()) : raw; });
    if (document.getElementById('cfg-pn-headers-' + cssId(id))) {
      const headers: ConfigObject = {};
      Object.setPrototypeOf(headers, null);
      const rows = pairs(readRows('cfg-pn-headers-' + cssId(id), 'pair'));
      editor.headerRows.set(id, rows);
      rows.forEach(([key, item]) => { headers[key] = item; });
      next['inject_headers'] = headers;
    }
    return next;
  }

  function commitCard(kind: Kind, id: string): void {
    const next = readCard(kind, id);
    if (!next) return;
    mapOf(kind)[id] = next;
    if (kind === 'panels') Object.keys(mapOf(kind)).forEach(key => { validateCard(kind, key); });
    else validateCard(kind, id);
    refreshCardTitle(kind, id);
    markDirty();
  }

  function refreshCardTitle(kind: Kind, id: string): void {
    const card = cardRoot(kind, id);
    const title = card?.querySelector('.panel-card-title');
    if (title) title.innerHTML = kind === 'inbounds'
      ? profileHeader(id, cardData(kind, id))
      : panelHeader(id, cardData(kind, id));
  }

  function renameCard(kind: Kind, oldId: string, raw: string): void {
    const nextId = raw.trim();
    const map = mapOf(kind);
    if (nextId === oldId) return;
    if (!nextId || Object.prototype.hasOwnProperty.call(map, nextId)) {
      validateCard(kind, oldId);
      return;
    }
    const value = readCard(kind, oldId);
    const ordered: ConfigObject = {};
    Object.setPrototypeOf(ordered, null);
    Object.keys(map).forEach(key => { ordered[key === oldId ? nextId : key] = (key === oldId ? value : map[key]) ?? null; });
    if (!editor.draft) return;
    if (kind === 'inbounds') editor.draft['profiles'] = ordered;
    else editor.draft['3xui'] = ordered;
    if (kind === 'inbounds') {
      for (const field of ['json', 'xhttpExtra']) {
        const oldKey = profileJsonKey(oldId, field);
        if (editor.jsonInputs.has(oldKey)) {
          const rawValue = editor.jsonInputs.get(oldKey);
          if (rawValue !== undefined) editor.jsonInputs.set(profileJsonKey(nextId, field), rawValue);
          editor.jsonInputs.delete(oldKey);
        }
      }
    }
    editor.open[kind].delete(oldId);
    if (kind === 'panels' && editor.headerRows.has(oldId)) {
      const rows = editor.headerRows.get(oldId);
      if (rows) editor.headerRows.set(nextId, rows);
      editor.headerRows.delete(oldId);
    }
    editor.open[kind].add(nextId);
    renderCards(kind);
    Object.keys(ordered).forEach(key => { validateCard(kind, key); });
    markDirty();
  }

  function toggleConfigCard(kind: Kind, id: string): void {
    const card = cardRoot(kind, id);
    if (!card) return;
    card.classList.toggle('open');
    card.querySelector('.config-card-toggle')?.setAttribute('aria-expanded', String(card.classList.contains('open')));
    if (card.classList.contains('open')) {
      editor.open[kind].add(id);
      if (kind === 'inbounds') enhanceJsonInputs(card);
      refreshJsonEditors(card);
    }
    else editor.open[kind].delete(id);
  }

  function init(): () => void {
    const controller = new AbortController();
    document.getElementById('tab-config')?.addEventListener('click', event => {
      if (!(event.target instanceof Element)) return;
      const header = event.target.closest('#tab-config .panel-card-header');
      if (!header || event.target.closest('button')) return;
      const card = header.closest<HTMLElement>('.panel-card');
      const kind = card?.dataset['kind'];
      const id = card?.getAttribute('data-id');
      if ((kind === 'inbounds' || kind === 'panels') && id !== null && id !== undefined) toggleConfigCard(kind, id);
    }, { signal: controller.signal });
    return () => { controller.abort(); };
  }

  async function deleteConfigCard(event: Event, kind: Kind, id: string): Promise<void> {
    event.stopPropagation();
    const map = mapOf(kind);
    const ok = await confirm(t('config_confirm_delete'), t('confirm'));
    if (!ok) return;
    Reflect.deleteProperty(map, id);
    if (kind === 'inbounds') {
      for (const field of ['json', 'xhttpExtra']) editor.jsonInputs.delete(profileJsonKey(id, field));
    }
    if (kind === 'panels') editor.headerRows.delete(id);
    editor.open[kind].delete(id);
    renderCards(kind);
    validateAll();
    markDirty();
  }

  return { init, renderCards, toggleConfigCard, deleteConfigCard };
}

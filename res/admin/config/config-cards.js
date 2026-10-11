import { destroyJsonEditors, refreshJsonEditors } from '../json-editor.js';
import { editor, INTEGER_RE } from './config-state.js';
import { ConfigNumber, isPlainObject, jsonText, parseJsonField } from './config-json.js';
import { attr, restartIcon, fieldError, passWrap, readText, enhanceJsonInputs, rowList, readRows, bindRows, bilingual, nodeOptions, cssId, cardRoot } from './config-controls.js';
import { validateCard, validateAll } from './config-validation.js';

function profileJsonKey(id, field) {
  return JSON.stringify([id, field]);
}

function profileJsonText(id, field, value) {
  return editor.jsonInputs.get(profileJsonKey(id, field)) ?? jsonText(value);
}

function profileHeader(id, profile) {
  const lang = window.lang === 'ru' ? 1 : 0;
  const name = Array.isArray(profile.name) ? (profile.name[lang] || profile.name[0] || '') : '';
  return `${escapeHtml(profile.flag || '')} <span class="panel-card-name">${escapeHtml(id)}</span> <span>${escapeHtml(name)}</span>`;
}

function panelHeader(id, panel) {
  return `<span class="panel-card-name">${escapeHtml(id)}</span> <span>${escapeHtml(panel.name || '')}</span> <span class="badge config-panel-mode" data-mode="${attr(panel.mode || '')}">${escapeHtml(panel.mode ? panel.mode + ' mode' : '')}</span>`;
}

function cardShell(kind, id, titleHtml, body, open) {
  const last = kind === 'panels' && Object.keys(editor.draft['3xui'] || {}).length <= 1;
  const safe = attr(id);
  return `<div class="panel-card${open ? ' open' : ''}" data-kind="${kind}" data-id="${safe}">
    <div class="panel-card-header">
      <div class="panel-card-title">${titleHtml}</div>
      <div class="panel-card-meta">
        ${kind === 'panels' ? restartIcon() : ''}
        <button type="button" class="btn-tiny danger config-card-delete" ${last ? 'disabled' : ''} title="${last ? attr(t('config_last_panel')) : ''}" ${last ? `aria-label="${attr(t('config_last_panel'))}"` : ''}>${escapeHtml(t('delete'))}</button>
        <button type="button" class="config-card-toggle" aria-expanded="${open}" aria-label="${safe}"><svg class="panel-card-arrow" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4,6 8,10 12,6"/></svg></button>
      </div>
    </div>
    <div class="panel-card-body">${body}</div>
  </div>`;
}

function inboundBody(id, profile) {
  const p = profile || {};
  return `<div class="form-grid">
    <div class="form-row"><label>id</label><div class="input-wrap"><input type="text" data-field="id" value="${attr(id)}" spellcheck="false">${fieldError('cfg-in-err-' + cssId(id) + '-id')}</div></div>
    <div class="form-row"><label>flag</label><div class="input-wrap"><input type="text" data-field="flag" value="${attr(p.flag ?? '')}">${fieldError('cfg-in-err-' + cssId(id) + '-flag')}</div></div>
    <div class="form-row"><label>name</label><div class="input-wrap">${bilingual('cfg-in-' + cssId(id) + '-name', p.name)}${fieldError('cfg-in-err-' + cssId(id) + '-name')}</div></div>
    <div class="form-row"><label>description</label><div class="input-wrap">${bilingual('cfg-in-' + cssId(id) + '-description', p.description)}${fieldError('cfg-in-err-' + cssId(id) + '-description')}</div></div>
    <div class="form-row"><label>shortProfileDescription</label><div class="input-wrap">${bilingual('cfg-in-' + cssId(id) + '-short', p.shortProfileDescription)}${fieldError('cfg-in-err-' + cssId(id) + '-shortProfileDescription')}</div></div>
    <div class="form-row"><label>whitelist</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist" ${p.whitelist ? 'checked' : ''}><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>node</label><div class="input-wrap"><select data-field="node" class="cfg-node-select">${nodeOptions(p.node || '')}</select>${fieldError('cfg-in-err-' + cssId(id) + '-node')}</div></div>
    <div class="form-row"><label>masterLink</label><div class="input-wrap"><input type="text" data-field="masterLink" value="${attr(p.masterLink ?? '')}" spellcheck="false">${fieldError('cfg-in-err-' + cssId(id) + '-masterLink')}</div></div>
    <div class="form-row"><label>json</label><div class="input-wrap"><textarea class="config-json" data-field="json" spellcheck="false">${escapeHtml(profileJsonText(id, 'json', p.json))}</textarea>${fieldError('cfg-in-err-' + cssId(id) + '-json')}</div></div>
    <div class="form-row"><label>xhttpExtra</label><div class="input-wrap"><textarea class="config-json" data-field="xhttpExtra" spellcheck="false">${escapeHtml(profileJsonText(id, 'xhttpExtra', p.xhttpExtra))}</textarea>${fieldError('cfg-in-err-' + cssId(id) + '-xhttpExtra')}</div></div>
  </div>`;
}

function panelBody(id, panel) {
  const p = panel || {};
  const auth = Array.isArray(p.nginx_auth) ? p.nginx_auth : null;
  const headers = isPlainObject(p.inject_headers) ? editor.headerRows.get(id) || Object.entries(p.inject_headers) : null;
  return `<div class="form-grid">
    <div class="form-row"><label>key</label><div class="input-wrap"><input type="text" data-field="id" value="${attr(id)}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-id')}</div></div>
    <div class="form-row"><label>name</label><div class="input-wrap"><input type="text" data-field="name" value="${attr(p.name ?? '')}">${fieldError('cfg-pn-err-' + cssId(id) + '-name')}</div></div>
    <div class="form-row"><label>address</label><div class="input-wrap"><input type="text" data-field="address" value="${attr(p.address ?? '')}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-address')}</div></div>
    <div class="form-row"><label>uri</label><div class="input-wrap"><input type="text" data-field="uri" value="${attr(p.uri ?? '')}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-uri')}</div></div>
    <div class="form-row"><label>port</label><div class="input-wrap"><input type="text" data-field="port" value="${attr(p.port ?? '')}" spellcheck="false">${fieldError('cfg-pn-err-' + cssId(id) + '-port')}</div></div>
    <div class="form-row"><label>token</label><div class="input-wrap">${passWrap('cfg-pn-token-' + cssId(id), p.token ?? '')}${fieldError('cfg-pn-err-' + cssId(id) + '-token')}</div></div>
    <div class="form-row"><label>https</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="https" ${p.https ? 'checked' : ''}><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>whitelist</label><div class="input-wrap"><label class="dark-check"><input type="checkbox" data-field="whitelist" ${p.whitelist ? 'checked' : ''}><div class="check-track"><div class="check-thumb"></div></div></label></div></div>
    <div class="form-row"><label>mode</label><div class="input-wrap"><select data-field="mode"><option value="blacklist"${p.mode === 'blacklist' ? ' selected' : ''}>blacklist mode</option><option value="whitelist"${p.mode === 'whitelist' ? ' selected' : ''}>whitelist mode</option></select>${fieldError('cfg-pn-err-' + cssId(id) + '-mode')}</div></div>
    <div class="form-row"><label>inbounds_list</label><div class="input-wrap" id="cfg-pn-inbounds-${cssId(id)}">${rowList('cfg-pn-rows-' + cssId(id), (p.inbounds_list || []).map(String), 'int')}${fieldError('cfg-pn-err-' + cssId(id) + '-inbounds_list')}</div></div>
    <div class="form-row"><label>nginx_auth</label><div class="input-wrap">${auth ? `<div class="config-pair"><input type="text" data-field="nginx0" value="${attr(auth[0] ?? '')}">${passWrap('cfg-pn-nginx-password-' + cssId(id), auth[1] ?? '')}</div><button type="button" class="btn-tiny danger" data-nested-remove="nginx_auth">${escapeHtml(t('config_remove'))}</button>` : `<div class="config-absent">${escapeHtml(t('config_not_set'))} <button type="button" class="btn-tiny" data-nested-add="nginx_auth">${escapeHtml(t('config_add'))}</button></div>`}${fieldError('cfg-pn-err-' + cssId(id) + '-nginx_auth')}</div></div>
    <div class="form-row"><label>inject_headers</label><div class="input-wrap">${headers ? `${rowList('cfg-pn-headers-' + cssId(id), headers, 'pair')}<button type="button" class="btn-tiny danger" data-nested-remove="inject_headers">${escapeHtml(t('config_remove_headers'))}</button>` : `<div class="config-absent">${escapeHtml(t('config_not_set'))} <button type="button" class="btn-tiny" data-nested-add="inject_headers">${escapeHtml(t('config_add'))}</button></div>`}${fieldError('cfg-pn-err-' + cssId(id) + '-inject_headers')}</div></div>
  </div>`;
}

export function createCardEditor({ markDirty, openNew }) {
  function renderCards(kind) {
    const pane = document.getElementById('config-pane-' + kind);
    destroyJsonEditors(pane);
    // Pending invalid renames and duplicate header rows belong to the draft too.
    const pendingIds = new Map([...pane.querySelectorAll('.panel-card')].map(card => [card.dataset.id, card.querySelector('[data-field="id"]')?.value]));
    const map = kind === 'inbounds' ? editor.draft.profiles : editor.draft['3xui'];
    const subtitle = kind === 'inbounds' ? t('config_inbounds_sub') : t('config_panels_sub');
    const cards = Object.entries(isPlainObject(map) ? map : {}).map(([id, value]) => {
      const open = editor.open[kind].has(id);
      const title = kind === 'inbounds' ? profileHeader(id, value) : panelHeader(id, value);
      const body = kind === 'inbounds' ? inboundBody(id, value) : panelBody(id, value);
      return cardShell(kind, id, title, body, open);
    }).join('');
    const icon = kind === 'panels' ? restartIcon() : '';
    pane.innerHTML = `<div class="config-subtitle">${icon}<span>${escapeHtml(subtitle)}</span></div>
      <div class="toolbar"><button type="button" class="btn btn-secondary" id="cfg-new-${kind}">${escapeHtml(t('config_new'))}</button></div>
      <div class="panel-cards">${cards}</div>`;
    document.getElementById('cfg-new-' + kind).onclick = () => openNew(kind);
    Object.keys(isPlainObject(map) ? map : {}).forEach(id => bindCard(kind, id));
    pendingIds.forEach((raw, id) => {
      const input = cardRoot(kind, id)?.querySelector('[data-field="id"]');
      if (input && raw !== undefined) input.value = raw;
    });
    initPassToggles();
    Object.keys(isPlainObject(map) ? map : {}).forEach(id => validateCard(kind, id));
  }

  function bindCard(kind, id) {
    const card = cardRoot(kind, id);
    if (!card) return;
    const commit = () => commitCard(kind, id);
    card.querySelector('.config-card-delete').onclick = event => deleteConfigCard(event, kind, id);
    card.querySelector('.config-card-toggle').onclick = () => toggleConfigCard(kind, id);
    card.querySelectorAll('input, textarea, select').forEach(input => {
      if (input.dataset.field === 'id') {
        input.addEventListener('input', () => { validateCard(kind, id); markDirty(); });
        input.addEventListener('blur', () => renameCard(kind, id, input.value));
        return;
      }
      input.addEventListener('input', commit);
      input.addEventListener('change', commit);
    });
    if (kind === 'inbounds' && card.classList.contains('open')) enhanceJsonInputs(card);
    if (kind === 'panels') {
      bindRows('cfg-pn-rows-' + cssId(id), 'int', (rows, live) => {
        editor.draft['3xui'][id].inbounds_list = rows.map(row => INTEGER_RE.test(row.trim()) ? new ConfigNumber(row.trim()) : row);
        if (!live) renderCards('panels');
        else validateCard('panels', id);
        markDirty();
      });
      bindRows('cfg-pn-headers-' + cssId(id), 'pair', (rows, live) => {
        editor.headerRows.set(id, rows);
        const headers = Object.create(null);
        rows.forEach(([key, value]) => { headers[key] = value; });
        editor.draft['3xui'][id].inject_headers = headers;
        if (!live) renderCards('panels');
        else validateCard('panels', id);
        markDirty();
      });
      card.querySelectorAll('[data-nested-add]').forEach(button => button.addEventListener('click', event => {
        const which = event.currentTarget.dataset.nestedAdd;
        if (which === 'nginx_auth') editor.draft['3xui'][id].nginx_auth = ['', ''];
        else editor.draft['3xui'][id].inject_headers = {};
        editor.open.panels.add(id);
        renderCards('panels');
        markDirty();
      }));
      card.querySelectorAll('[data-nested-remove]').forEach(button => button.addEventListener('click', event => {
        const which = event.currentTarget.dataset.nestedRemove;
        if (which === 'inject_headers') editor.headerRows.delete(id);
        delete editor.draft['3xui'][id][which];
        editor.open.panels.add(id);
        renderCards('panels');
        markDirty();
      }));
    }
  }

  function readCard(kind, id) {
    const card = cardRoot(kind, id);
    if (!card) return null;
    const value = input => card.querySelector(`[data-field="${input}"]`);
    if (kind === 'inbounds') {
      const current = editor.draft.profiles[id] || {};
      const next = { ...current };
      next.flag = value('flag')?.value ?? '';
      next.whitelist = !!value('whitelist')?.checked;
      next.node = value('node')?.value ?? '';
      next.masterLink = value('masterLink')?.value ?? '';
      next.name = [document.getElementById('cfg-in-' + cssId(id) + '-name-0')?.value ?? '', document.getElementById('cfg-in-' + cssId(id) + '-name-1')?.value ?? ''];
      next.description = [document.getElementById('cfg-in-' + cssId(id) + '-description-0')?.value ?? '', document.getElementById('cfg-in-' + cssId(id) + '-description-1')?.value ?? ''];
      next.shortProfileDescription = [document.getElementById('cfg-in-' + cssId(id) + '-short-0')?.value ?? '', document.getElementById('cfg-in-' + cssId(id) + '-short-1')?.value ?? ''];
      for (const field of ['json', 'xhttpExtra']) {
        const raw = value(field)?.value;
        if (raw !== undefined) editor.jsonInputs.set(profileJsonKey(id, field), raw);
        next[field] = parseJsonField(raw, current[field]);
      }
      return next;
    }
    const current = editor.draft['3xui'][id] || {};
    const next = { ...current };
    next.name = value('name')?.value ?? '';
    next.address = value('address')?.value ?? '';
    next.uri = value('uri')?.value ?? '';
    const port = value('port')?.value ?? '';
    next.port = port === String(current.port) ? current.port : INTEGER_RE.test(port) ? new ConfigNumber(port) : port;
    const token = document.getElementById('cfg-pn-token-' + cssId(id));
    if (token) next.token = token.value;
    next.https = !!value('https')?.checked;
    next.whitelist = !!value('whitelist')?.checked;
    next.mode = value('mode')?.value || 'blacklist';
    next.inbounds_list = readRows('cfg-pn-rows-' + cssId(id), 'int').map(row => INTEGER_RE.test(row.trim()) ? new ConfigNumber(row.trim()) : row);
    if (value('nginx0')) next.nginx_auth = [value('nginx0').value, readText('cfg-pn-nginx-password-' + cssId(id))];
    if (document.getElementById('cfg-pn-headers-' + cssId(id))) {
      const headers = Object.create(null);
      const rows = readRows('cfg-pn-headers-' + cssId(id), 'pair');
      editor.headerRows.set(id, rows);
      rows.forEach(([key, item]) => { headers[key] = item; });
      next.inject_headers = headers;
    }
    return next;
  }

  function commitCard(kind, id) {
    const next = readCard(kind, id);
    if (!next) return;
    if (kind === 'inbounds') editor.draft.profiles[id] = next;
    else editor.draft['3xui'][id] = next;
    validateCard(kind, id);
    refreshCardTitle(kind, id);
    markDirty();
  }

  function refreshCardTitle(kind, id) {
    const card = cardRoot(kind, id);
    const title = card?.querySelector('.panel-card-title');
    if (title) title.innerHTML = kind === 'inbounds'
      ? profileHeader(id, editor.draft.profiles[id] || {})
      : panelHeader(id, editor.draft['3xui'][id] || {});
  }

  function renameCard(kind, oldId, raw) {
    const nextId = raw.trim();
    const map = kind === 'inbounds' ? editor.draft.profiles : editor.draft['3xui'];
    if (nextId === oldId) return;
    if (!nextId || Object.prototype.hasOwnProperty.call(map, nextId)) {
      validateCard(kind, oldId);
      return;
    }
    const value = readCard(kind, oldId);
    const ordered = Object.create(null);
    Object.keys(map).forEach(key => { ordered[key === oldId ? nextId : key] = key === oldId ? value : map[key]; });
    if (kind === 'inbounds') editor.draft.profiles = ordered;
    else editor.draft['3xui'] = ordered;
    if (kind === 'inbounds') {
      for (const field of ['json', 'xhttpExtra']) {
        const oldKey = profileJsonKey(oldId, field);
        if (editor.jsonInputs.has(oldKey)) {
          editor.jsonInputs.set(profileJsonKey(nextId, field), editor.jsonInputs.get(oldKey));
          editor.jsonInputs.delete(oldKey);
        }
      }
    }
    editor.open[kind].delete(oldId);
    if (kind === 'panels' && editor.headerRows.has(oldId)) {
      editor.headerRows.set(nextId, editor.headerRows.get(oldId));
      editor.headerRows.delete(oldId);
    }
    editor.open[kind].add(nextId);
    renderCards(kind);
    Object.keys(ordered).forEach(key => validateCard(kind, key));
    markDirty();
  }

  function toggleConfigCard(kind, id) {
    const card = cardRoot(kind, id);
    if (!card) return;
    card.classList.toggle('open');
    card.querySelector('.config-card-toggle').setAttribute('aria-expanded', String(card.classList.contains('open')));
    if (card.classList.contains('open')) {
      editor.open[kind].add(id);
      if (kind === 'inbounds') enhanceJsonInputs(card);
      refreshJsonEditors(card);
    }
    else editor.open[kind].delete(id);
  }

  document.addEventListener('click', event => {
    const header = event.target.closest('#tab-config .panel-card-header');
    if (!header || event.target.closest('button')) return;
    const card = header.closest('.panel-card');
    if (!card) return;
    toggleConfigCard(card.dataset.kind, card.getAttribute('data-id'));
  });

  async function deleteConfigCard(event, kind, id) {
    event.stopPropagation();
    const map = kind === 'inbounds' ? editor.draft.profiles : editor.draft['3xui'];
    if (kind === 'panels' && Object.keys(map).length <= 1) return;
    const ok = await confirmDialog(t('config_confirm_delete'), t('confirm'));
    if (!ok) return;
    delete map[id];
    if (kind === 'inbounds') {
      for (const field of ['json', 'xhttpExtra']) editor.jsonInputs.delete(profileJsonKey(id, field));
    }
    if (kind === 'panels') editor.headerRows.delete(id);
    editor.open[kind].delete(id);
    renderCards(kind);
    validateAll();
    markDirty();
  }

  return { renderCards, toggleConfigCard, deleteConfigCard };
}

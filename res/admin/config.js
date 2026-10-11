const RESTART_KEYS = ['uri', 'api_uri', 'api_token', '3xui', 'redis', 'bot', 'publicbot', 'panel_alert_cooldown', 'salt'];
const OPTIONAL_KEYS = ['api_uri', 'fallback_domain', 'funny_strings', 'bot', 'publicbot'];
const REQUIRED_KEYS = [
  'uri', 'api_token', 'api_admin_ui_auth', 'provider_id', 'panel_alert_cooldown', 'salt',
  'fingerprints', 'nodes', 'json_template', 'bypass_packages', 'domain', 'ping_check_url',
  '3xui', 'sub_name', 'profiles', 'redis',
];
const KNOWN_KEYS = ['$schema', ...REQUIRED_KEYS, ...OPTIONAL_KEYS.filter(k => !REQUIRED_KEYS.includes(k))];
const TEMPLATE_KEYS = ['dns', 'routing', 'inbounds', 'outbounds', 'remarks'];
const PROFILE_FIELDS = ['flag', 'name', 'json', 'description', 'whitelist', 'xhttpExtra', 'masterLink', 'node', 'shortProfileDescription'];
const PANEL_FIELDS = ['name', 'address', 'port', 'uri', 'token', 'https', 'whitelist', 'inbounds_list', 'mode'];
const PANEL_OPTIONAL = ['nginx_auth', 'inject_headers'];
const INTEGER_RE = /^(0|-?[1-9]\d*)$/;

const MAIN_SECTIONS = [
  ['config_sec_service', ['uri', 'api_uri', 'domain', 'fallback_domain', 'ping_check_url', 'provider_id', 'sub_name']],
  ['config_sec_auth', ['api_token', 'api_admin_ui_auth', 'salt']],
  ['config_sec_lists', ['panel_alert_cooldown', 'fingerprints', 'bypass_packages', 'funny_strings', 'nodes']],
  ['config_sec_integrations', ['redis', 'bot', 'publicbot']],
  ['config_sec_template', ['json_template', '$schema']],
];

const editor = {
  loaded: null,
  draft: null,
  etag: '',
  dirty: false,
  conflict: false,
  saving: false,
  loading: false,
  fetched: false,
  open: { inbounds: new Set(), panels: new Set() },
  restartLine: '',
  dialog: null,
  nodeRows: null,
  headerRows: new Map(),
  // Textarea source is separate from parsed values, including JSON strings.
  jsonInputs: new Map(),
};

// Preserve Python's integer/float distinction and integers beyond JS's safe range.
class ConfigNumber {
  constructor(raw) { this.raw = raw; }
  toString() { return this.raw; }
}

const configKeyOrder = new WeakMap();

function parseConfigJson(text) {
  let pos = 0;
  const whitespace = () => { while (pos < text.length && /[\x20\t\r\n]/.test(text[pos])) pos++; };
  const value = () => {
    whitespace();
    const start = pos;
    if (text[pos] === '"') {
      pos++;
      while (pos < text.length) {
        if (text[pos++] === '\\') pos++;
        else if (text[pos - 1] === '"') return JSON.parse(text.slice(start, pos));
      }
      throw new SyntaxError('Invalid JSON');
    }
    if (text[pos] === '{' || text[pos] === '[') {
      const object = text[pos++] === '{';
      const result = object ? Object.create(null) : [];
      if (object) configKeyOrder.set(result, []);
      const end = object ? '}' : ']';
      whitespace();
      if (text[pos] === end) { pos++; return result; }
      while (true) {
        if (object) {
          whitespace();
          if (text[pos] !== '"') throw new SyntaxError('Invalid JSON');
          const key = value();
          whitespace();
          if (text[pos++] !== ':') throw new SyntaxError('Invalid JSON');
          if (!Object.hasOwn(result, key)) configKeyOrder.get(result).push(key);
          result[key] = value();
        } else result.push(value());
        whitespace();
        if (text[pos] === end) { pos++; return result; }
        if (text[pos++] !== ',') throw new SyntaxError('Invalid JSON');
      }
    }
    for (const [token, result] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(token, pos)) { pos += token.length; return result; }
    }
    const number = text.slice(pos).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (!number) throw new SyntaxError('Invalid JSON');
    if (/[.eE]/.test(number[0]) && !Number.isFinite(Number(number[0]))) throw new SyntaxError('Non-finite JSON number');
    pos += number[0].length;
    return new ConfigNumber(number[0]);
  };
  const result = value();
  whitespace();
  if (pos !== text.length) throw new SyntaxError('Invalid JSON');
  return result;
}

function stringifyConfig(value, pretty = false, depth = 0) {
  if (value instanceof ConfigNumber) return value.raw;
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  const array = Array.isArray(value);
  const entries = array ? value.map(item => stringifyConfig(item, pretty, depth + 1))
    : [...new Set([...(configKeyOrder.get(value) || []), ...Object.keys(value)])]
      .filter(key => Object.hasOwn(value, key))
      .map(key => JSON.stringify(key) + (pretty ? ': ' : ':') + stringifyConfig(value[key], pretty, depth + 1));
  const [open, close] = array ? ['[', ']'] : ['{', '}'];
  if (!pretty || !entries.length) return open + entries.join(',') + close;
  return open + '\n' + entries.map(item => '  '.repeat(depth + 1) + item).join(',\n') + '\n' + '  '.repeat(depth) + close;
}

function clone(value) {
  return value === undefined ? undefined : parseConfigJson(stringifyConfig(value));
}

function jsonEqual(left, right) {
  if (left instanceof ConfigNumber || right instanceof ConfigNumber) {
    if (!(left instanceof ConfigNumber) || !(right instanceof ConfigNumber)) return false;
    const integer = raw => !/[.eE]/.test(raw);
    if (integer(left.raw) !== integer(right.raw)) return false;
    return integer(left.raw) ? BigInt(left.raw) === BigInt(right.raw) : Number(left.raw) === Number(right.raw);
  }
  if (left === undefined && right === undefined) return true;
  if (left === undefined || right === undefined) return false;
  if (Object.prototype.toString.call(left) !== Object.prototype.toString.call(right)) return false;
  if (left && typeof left === 'object') {
    if (Array.isArray(left)) {
      if (left.length !== right.length) return false;
      return left.every((item, i) => jsonEqual(item, right[i]));
    }
    const keys = Object.keys(left);
    if (keys.length !== Object.keys(right).length) return false;
    return keys.every(key => Object.prototype.hasOwnProperty.call(right, key) && jsonEqual(left[key], right[key]));
  }
  return left === right;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && !(value instanceof ConfigNumber);
}

function isInteger(value) {
  return value instanceof ConfigNumber ? !/[.eE]/.test(value.raw) : typeof value === 'number' && Number.isInteger(value);
}

function isFiniteNumber(value) {
  return value instanceof ConfigNumber ? Number.isFinite(Number(value.raw)) : typeof value === 'number' && Number.isFinite(value);
}

function attr(value) {
  return escapeHtml(value).replace(/`/g, '&#96;');
}

function restartIcon() {
  return `<span class="config-restart-wrap"><button type="button" class="config-restart-icon" aria-label="${attr(t('config_restart_tip'))}">!</button><span class="config-restart-tooltip" role="tooltip">${escapeHtml(t('config_restart_tip'))}</span></span>`;
}

function fieldError(id) {
  return `<div class="config-error" id="${id}" hidden></div>`;
}

function passWrap(id, value) {
  return `<div class="config-pass">
    <input type="password" id="${id}" autocomplete="new-password" spellcheck="false" value="${attr(value)}">
    <button type="button" class="pass-toggle" onclick="revealConfigPass('${id}', this)" data-t-aria="toggle_password" aria-label="${attr(t('toggle_password'))}"></button>
  </div>`;
}

function setError(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || '';
}

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
  // jsonschema messages can quote values, including whole objects with secrets.
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

export function revealConfigPass(id, btn) {
  togglePass(id, btn);
}

function readText(id) {
  return document.getElementById(id)?.value ?? '';
}

function commitMainField(key) {
  if (!editor.draft) return;
  if (key === 'api_uri' || key === 'fallback_domain') {
    if (!Object.prototype.hasOwnProperty.call(editor.draft, key)) return;
    editor.draft[key] = readText('cfg-' + key);
  } else if (key === 'panel_alert_cooldown') {
    const raw = readText('cfg-panel_alert_cooldown').trim();
    editor.draft.panel_alert_cooldown = INTEGER_RE.test(raw) ? new ConfigNumber(raw) : raw;
  } else if (['uri', 'domain', 'ping_check_url', 'provider_id', 'sub_name', 'api_token', 'salt'].includes(key)) {
    editor.draft[key] = readText('cfg-' + key);
  } else if (key === 'api_admin_ui_auth') {
    editor.draft.api_admin_ui_auth = [readText('cfg-auth-0'), readText('cfg-auth-1')];
  } else if (key === 'redis') {
    const timeout = readText('cfg-redis-timeout').trim();
    const redis = { url: readText('cfg-redis-url') };
    if (timeout !== '') {
      try { redis.socket_timeout = parseConfigJson(timeout); } catch { redis.socket_timeout = timeout; }
    }
    editor.draft.redis = redis;
  } else if (key === 'json_template') {
    const raw = readText('cfg-json_template');
    editor.jsonInputs.set('json_template', raw);
    try { editor.draft.json_template = parseConfigJson(raw); } catch { editor.draft.json_template = raw; }
  }
  validateField(key);
  markDirty();
}

function prettyTemplate(force) {
  const input = document.getElementById('cfg-json_template');
  if (!input) return;
  try {
    const parsed = parseConfigJson(input.value);
    if (force || isPlainObject(parsed)) input.value = stringifyConfig(parsed, true);
    editor.draft.json_template = parsed;
  } catch {
    editor.draft.json_template = input.value;
  }
  editor.jsonInputs.set('json_template', input.value);
  validateField('json_template');
  markDirty();
}

function rowList(id, rows, kind) {
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

function readRows(id, kind) {
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

function bindRows(id, kind, apply) {
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

function stringRows(key) {
  const rows = Array.isArray(editor.draft[key]) ? editor.draft[key] : [];
  return rowList('cfg-rows-' + key, rows.map(item => String(item ?? '')), 'string');
}

function applyStringRows(key, rows, live) {
  editor.draft[key] = rows;
  if (!live) paintBox(key);
  validateField(key);
  markDirty();
}

function nodeRows() {
  const nodes = isPlainObject(editor.draft.nodes) ? editor.draft.nodes : {};
  return rowList('cfg-rows-nodes', editor.nodeRows || Object.entries(nodes), 'pair');
}

function applyNodeRows(rows, live) {
  editor.nodeRows = rows;
  const nodes = Object.create(null);
  rows.forEach(([key, value]) => { nodes[key] = value; });
  editor.draft.nodes = nodes;
  if (!live) paintBox('nodes');
  refreshNodeSelects();
  validateField('nodes');
  markDirty();
}

function paintBox(key) {
  const slot = document.getElementById('cfg-box-' + key.replace('$', ''));
  if (!slot) return;
  const focus = document.activeElement;
  const focusId = focus && slot.contains(focus) ? focus.id : '';
  slot.outerHTML = boxHtml(key);
  bindBox(key);
  if (focusId) document.getElementById(focusId)?.focus();
}

function boxHtml(key) {
  const present = Object.prototype.hasOwnProperty.call(editor.draft, key);
  const optional = OPTIONAL_KEYS.includes(key);
  const restart = RESTART_KEYS.includes(key);
  const safeKey = key.replace('$', '');
  const label = `<div class="config-box-label"><span>${escapeHtml(key)}</span>${restart ? restartIcon() : ''}</div>`;
  const hint = `<div class="form-hint">${escapeHtml(t('config_hint_' + safeKey))}</div>`;
  let control = '';
  if (optional && !present) {
    control = `<div class="config-absent">${escapeHtml(t('config_not_set'))} <button type="button" class="btn-tiny" data-optional-add="${key}">${escapeHtml(t('config_add'))}</button></div>`;
  } else if (['uri', 'domain', 'ping_check_url', 'provider_id', 'sub_name', 'api_uri', 'fallback_domain'].includes(key)) {
    control = `<input type="text" id="cfg-${key}" value="${attr(editor.draft[key] ?? '')}" spellcheck="false">`;
  } else if (key === 'api_token' || key === 'salt') {
    control = passWrap('cfg-' + key, editor.draft[key] ?? '');
  } else if (key === 'api_admin_ui_auth') {
    const auth = Array.isArray(editor.draft.api_admin_ui_auth) ? editor.draft.api_admin_ui_auth : ['', ''];
    control = `<input type="text" id="cfg-auth-0" value="${attr(auth[0] ?? '')}" spellcheck="false" autocomplete="off">
      ${passWrap('cfg-auth-1', auth[1] ?? '')}`;
  } else if (key === 'panel_alert_cooldown') {
    control = `<input type="number" id="cfg-panel_alert_cooldown" step="1" value="${attr(editor.draft.panel_alert_cooldown ?? '')}">`;
  } else if (['fingerprints', 'bypass_packages', 'funny_strings'].includes(key)) {
    control = stringRows(key);
  } else if (key === 'nodes') {
    control = nodeRows();
  } else if (key === 'redis') {
    const redis = isPlainObject(editor.draft.redis) ? editor.draft.redis : {};
    const timeout = Object.prototype.hasOwnProperty.call(redis, 'socket_timeout') ? redis.socket_timeout : '';
    control = `<label class="config-sub">${escapeHtml(t('config_redis_url'))}</label>
      <input type="text" id="cfg-redis-url" value="${attr(redis.url ?? '')}" spellcheck="false">
      <label class="config-sub">${escapeHtml(t('config_redis_timeout'))}</label>
      <input type="number" id="cfg-redis-timeout" min="0" step="any" value="${attr(timeout)}">`;
  } else if (key === 'bot' || key === 'publicbot') {
    const obj = isPlainObject(editor.draft[key]) ? editor.draft[key] : { token: '' };
    control = `<label class="config-sub">${escapeHtml(t('token'))}</label>${passWrap('cfg-' + key + '-token', obj.token ?? '')}`;
    if (key === 'bot') {
      const rows = Array.isArray(obj.whitelist) ? obj.whitelist.map(item => String(item)) : [];
      control += `<label class="config-sub">${escapeHtml(t('config_whitelist'))}</label>${rowList('cfg-rows-bot', rows, 'int')}`;
    }
    control += `<button type="button" class="btn-tiny danger" data-optional-remove="${key}">${escapeHtml(t('config_remove'))}</button>`;
  } else if (key === 'json_template') {
    const value = editor.jsonInputs.get('json_template') ?? jsonText(editor.draft.json_template);
    control = `<textarea id="cfg-json_template" class="config-json" spellcheck="false">${escapeHtml(value)}</textarea>`;
  } else if (key === '$schema') {
    control = `<div class="config-locked">${escapeHtml(editor.draft.$schema ?? '')}</div>`;
  }
  const remove = optional && present && key !== 'bot' && key !== 'publicbot'
    ? `<button type="button" class="btn-tiny danger" data-optional-remove="${key}">${escapeHtml(t('config_remove'))}</button>`
    : '';
  return `<div class="config-box" id="cfg-box-${safeKey}">${label}${hint}${control}${remove}${fieldError('cfg-err-' + safeKey)}</div>`;
}

function bindBox(key) {
  const slot = document.getElementById('cfg-box-' + key.replace('$', ''));
  if (!slot) return;
  initPassToggles();
  slot.querySelector('[data-optional-add]')?.addEventListener('click', () => addOptional(key));
  slot.querySelector('[data-optional-remove]')?.addEventListener('click', () => removeOptional(key));
  if (['fingerprints', 'bypass_packages', 'funny_strings'].includes(key)) {
    bindRows('cfg-rows-' + key, 'string', (rows, live) => applyStringRows(key, rows, live));
  }
  if (key === 'nodes') bindRows('cfg-rows-nodes', 'pair', applyNodeRows);
  if (key === 'bot') {
    bindRows('cfg-rows-bot', 'int', (rows, live) => {
      const ints = rows.map(row => INTEGER_RE.test(row.trim()) ? new ConfigNumber(row.trim()) : row);
      editor.draft.bot.whitelist = ints;
      if (!live) paintBox('bot');
      validateField('bot');
      markDirty();
    });
    document.getElementById('cfg-bot-token')?.addEventListener('input', () => {
      editor.draft.bot.token = readText('cfg-bot-token');
      validateField('bot');
      markDirty();
    });
  }
  if (key === 'publicbot') {
    document.getElementById('cfg-publicbot-token')?.addEventListener('input', () => {
      editor.draft.publicbot.token = readText('cfg-publicbot-token');
      validateField('publicbot');
      markDirty();
    });
  }
  const input = document.getElementById('cfg-' + key);
  if (input && key !== 'json_template') {
    input.addEventListener('input', () => commitMainField(key));
    input.addEventListener('blur', () => commitMainField(key));
  }
  const related = key === 'api_admin_ui_auth' ? ['cfg-auth-0', 'cfg-auth-1']
    : key === 'redis' ? ['cfg-redis-url', 'cfg-redis-timeout'] : [];
  related.forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => commitMainField(key));
  });
  const template = key === 'json_template' ? document.getElementById('cfg-json_template') : null;
  if (template) {
    template.addEventListener('input', () => {
      commitMainField('json_template');
    });
    template.addEventListener('blur', () => prettyTemplate(false));
  }
}

function addOptional(key) {
  if (key === 'api_uri' || key === 'fallback_domain') editor.draft[key] = '';
  else if (key === 'funny_strings') editor.draft[key] = [];
  else if (key === 'bot') editor.draft.bot = { token: '', whitelist: [] };
  else if (key === 'publicbot') editor.draft.publicbot = { token: '' };
  paintBox(key);
  validateField(key);
  markDirty();
}

function removeOptional(key) {
  delete editor.draft[key];
  paintBox(key);
  validateField(key);
  markDirty();
}

function renderMain() {
  const pane = document.getElementById('config-pane-main');
  pane.innerHTML = MAIN_SECTIONS.map(([title, keys]) => `
    <div class="section-title">${escapeHtml(t(title))}</div>
    ${keys.map(boxHtml).join('')}
  `).join('');
  MAIN_SECTIONS.forEach(([, keys]) => keys.forEach(bindBox));
}

function bilingual(prefix, pair) {
  const values = Array.isArray(pair) ? pair : ['', ''];
  return `<div class="config-pair">
    <input type="text" id="${prefix}-0" value="${attr(values[0] ?? '')}" data-t-ph="config_en" placeholder="${attr(t('config_en'))}">
    <input type="text" id="${prefix}-1" value="${attr(values[1] ?? '')}" data-t-ph="config_ru" placeholder="${attr(t('config_ru'))}">
  </div>`;
}

function nodeOptions(selected) {
  const keys = isPlainObject(editor.draft.nodes) ? Object.keys(editor.draft.nodes) : [];
  const options = keys.map(key => `<option value="${attr(key)}"${key === selected ? ' selected' : ''}>${escapeHtml(key)}</option>`);
  if (selected && !keys.includes(selected)) options.unshift(`<option value="${attr(selected)}" selected>${escapeHtml(selected)}</option>`);
  if (!selected) options.unshift(`<option value="" selected></option>`);
  return options.join('');
}

function jsonText(value) {
  try { return stringifyConfig(value ?? {}, true); } catch { return ''; }
}

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
  return `<span class="panel-card-name">${escapeHtml(id)}</span> <span>${escapeHtml(panel.name || '')}</span> <span class="badge">${escapeHtml(panel.mode || '')}</span>`;
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

function cssId(id) {
  return Array.from(id, char => char.codePointAt(0).toString(16)).join('-');
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
    <div class="form-row"><label>mode</label><div class="input-wrap"><select data-field="mode"><option value="blacklist"${p.mode === 'blacklist' ? ' selected' : ''}>blacklist</option><option value="whitelist"${p.mode === 'whitelist' ? ' selected' : ''}>whitelist</option></select>${fieldError('cfg-pn-err-' + cssId(id) + '-mode')}</div></div>
    <div class="form-row"><label>inbounds_list</label><div class="input-wrap" id="cfg-pn-inbounds-${cssId(id)}">${rowList('cfg-pn-rows-' + cssId(id), (p.inbounds_list || []).map(String), 'int')}${fieldError('cfg-pn-err-' + cssId(id) + '-inbounds_list')}</div></div>
    <div class="form-row"><label>nginx_auth</label><div class="input-wrap">${auth ? `<div class="config-pair"><input type="text" data-field="nginx0" value="${attr(auth[0] ?? '')}">${passWrap('cfg-pn-nginx-password-' + cssId(id), auth[1] ?? '')}</div><button type="button" class="btn-tiny danger" data-nested-remove="nginx_auth">${escapeHtml(t('config_remove'))}</button>` : `<div class="config-absent">${escapeHtml(t('config_not_set'))} <button type="button" class="btn-tiny" data-nested-add="nginx_auth">${escapeHtml(t('config_add'))}</button></div>`}${fieldError('cfg-pn-err-' + cssId(id) + '-nginx_auth')}</div></div>
    <div class="form-row"><label>inject_headers</label><div class="input-wrap">${headers ? `${rowList('cfg-pn-headers-' + cssId(id), headers, 'pair')}<button type="button" class="btn-tiny danger" data-nested-remove="inject_headers">${escapeHtml(t('config_remove_headers'))}</button>` : `<div class="config-absent">${escapeHtml(t('config_not_set'))} <button type="button" class="btn-tiny" data-nested-add="inject_headers">${escapeHtml(t('config_add'))}</button></div>`}${fieldError('cfg-pn-err-' + cssId(id) + '-inject_headers')}</div></div>
  </div>`;
}

function renderCards(kind) {
  const pane = document.getElementById('config-pane-' + kind);
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

function cardRoot(kind, id) {
  return [...document.querySelectorAll('#tab-config .panel-card')].find(card => card.dataset.kind === kind && card.getAttribute('data-id') === id);
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
    if (input.dataset.field === 'json' || input.dataset.field === 'xhttpExtra') {
      input.addEventListener('blur', () => prettyCardJson(kind, id, input));
    }
  });
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

function parseJsonField(raw, fallback) {
  if (raw == null) return fallback;
  try { return parseConfigJson(raw); } catch { return raw; }
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

function prettyCardJson(kind, id, input) {
  try {
    const parsed = parseConfigJson(input.value);
    if (isPlainObject(parsed)) input.value = stringifyConfig(parsed, true);
  } catch { /* keep the raw text so the field error stays visible */ }
  commitCard(kind, id);
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

export function toggleConfigCard(kind, id) {
  const card = cardRoot(kind, id);
  if (!card) return;
  card.classList.toggle('open');
  card.querySelector('.config-card-toggle').setAttribute('aria-expanded', String(card.classList.contains('open')));
  if (card.classList.contains('open')) editor.open[kind].add(id);
  else editor.open[kind].delete(id);
}

document.addEventListener('click', event => {
  const header = event.target.closest('#tab-config .panel-card-header');
  if (!header || event.target.closest('button')) return;
  const card = header.closest('.panel-card');
  if (!card) return;
  toggleConfigCard(card.dataset.kind, card.getAttribute('data-id'));
});

export async function deleteConfigCard(event, kind, id) {
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

function refreshNodeSelects() {
  document.querySelectorAll('.cfg-node-select').forEach(select => {
    const current = select.value;
    select.innerHTML = nodeOptions(current);
  });
  Object.keys(editor.draft.profiles || {}).forEach(id => validateCard('inbounds', id));
  validateField('nodes');
}

function issue(path, message) {
  return { path, message };
}

function validateDraft(draft) {
  const issues = [];
  ['uri', 'api_uri', 'fallback_domain', 'domain', 'ping_check_url', 'provider_id', 'sub_name', 'api_token', 'salt', '$schema'].forEach(key => {
    if (Object.hasOwn(draft, key) && typeof draft[key] !== 'string') issues.push(issue(key, t('config_err_required')));
  });
  ['redis', 'bot', 'publicbot'].forEach(key => {
    if (Object.hasOwn(draft, key) && !isPlainObject(draft[key])) issues.push(issue(key, t('config_err_required')));
  });
  if (!Array.isArray(draft.api_admin_ui_auth)) issues.push(issue('api_admin_ui_auth', t('config_err_required')));
  const keys = Object.keys(draft);
  keys.forEach(key => {
    if (!KNOWN_KEYS.includes(key)) issues.push(issue(key, t('config_err_unknown')));
  });
  REQUIRED_KEYS.forEach(key => {
    if (!Object.prototype.hasOwnProperty.call(draft, key)) issues.push(issue(key, t('config_err_required')));
  });
  if (editor.loaded && Object.prototype.hasOwnProperty.call(draft, '$schema') && !jsonEqual(draft.$schema, editor.loaded.$schema)) {
    issues.push(issue('$schema', t('config_schema_locked')));
  }
  if (typeof draft.api_token === 'string' && draft.api_token === '') issues.push(issue('api_token', t('config_err_required')));
  const auth = draft.api_admin_ui_auth;
  if (Array.isArray(auth) && (auth.length !== 2 || auth.some(item => typeof item !== 'string' || item === ''))) {
    issues.push(issue('api_admin_ui_auth', t('config_err_required')));
  }
  if (Object.prototype.hasOwnProperty.call(draft, 'panel_alert_cooldown') && !isInteger(draft.panel_alert_cooldown)) {
    issues.push(issue('panel_alert_cooldown', t('config_err_integer')));
  }
  ['fingerprints', 'bypass_packages'].forEach(key => {
    const list = draft[key];
    if (!Array.isArray(list) || list.some(item => typeof item !== 'string')) issues.push(issue(key, t('config_err_required')));
    else if (key === 'fingerprints' && new Set(list).size !== list.length) issues.push(issue(key, t('config_err_unique')));
  });
  if (Object.prototype.hasOwnProperty.call(draft, 'funny_strings')) {
    const list = draft.funny_strings;
    if (!Array.isArray(list) || list.some(item => typeof item !== 'string')) issues.push(issue('funny_strings', t('config_err_required')));
  }
  if (!isPlainObject(draft.nodes) || Object.entries(draft.nodes).some(([key, value]) => !key || typeof value !== 'string')) {
    issues.push(issue('nodes', t('config_err_required')));
  }
  if (isPlainObject(draft.redis)) {
    const extra = Object.keys(draft.redis).filter(key => key !== 'url' && key !== 'socket_timeout');
    if (typeof draft.redis.url !== 'string' || draft.redis.url === '') issues.push(issue('redis', t('config_err_required')));
    if (Object.prototype.hasOwnProperty.call(draft.redis, 'socket_timeout') && !(isFiniteNumber(draft.redis.socket_timeout) && draft.redis.socket_timeout > 0)) {
      issues.push(issue('redis', t('config_err_integer')));
    }
    if (extra.length) issues.push(issue('redis', t('config_err_unknown')));
  }
  if (Object.prototype.hasOwnProperty.call(draft, 'bot') && isPlainObject(draft.bot)) {
    const extra = Object.keys(draft.bot).filter(key => key !== 'token' && key !== 'whitelist');
    if (typeof draft.bot.token !== 'string') issues.push(issue('bot', t('config_err_required')));
    if (!Array.isArray(draft.bot.whitelist) || draft.bot.whitelist.some(item => !isInteger(item))) issues.push(issue('bot', t('config_err_integer')));
    if (extra.length) issues.push(issue('bot', t('config_err_unknown')));
  }
  if (Object.prototype.hasOwnProperty.call(draft, 'publicbot') && isPlainObject(draft.publicbot)) {
    if (typeof draft.publicbot.token !== 'string' || Object.keys(draft.publicbot).some(key => key !== 'token')) {
      issues.push(issue('publicbot', t('config_err_required')));
    }
  }
  if (typeof draft.json_template === 'string') issues.push(issue('json_template', t('config_err_json')));
  else if (isPlainObject(draft.json_template)) {
    TEMPLATE_KEYS.forEach(key => {
      if (!Object.prototype.hasOwnProperty.call(draft.json_template, key)) issues.push(issue('json_template', t('config_err_required')));
    });
    ['routing', 'servers'].forEach(key => {
      if (Object.prototype.hasOwnProperty.call(draft.json_template, key) && !isPlainObject(draft.json_template[key])) {
        issues.push(issue('json_template', t('config_err_json')));
      }
    });
    if (isPlainObject(draft.json_template.routing) && ['rules', 'domainStrategy'].some(key => !Object.hasOwn(draft.json_template.routing, key))) {
      issues.push(issue('json_template', t('config_err_required')));
    }
    const servers = draft.json_template.servers;
    if (isPlainObject(servers) && (!Array.isArray(servers.servers) || typeof servers.queryStrategy !== 'string' || Object.keys(servers).some(key => !['servers', 'queryStrategy'].includes(key)))) {
      issues.push(issue('json_template', t('config_err_json')));
    }
  } else if (draft.json_template !== undefined) issues.push(issue('json_template', t('config_err_json')));

  const nodes = isPlainObject(draft.nodes) ? draft.nodes : {};
  const profiles = isPlainObject(draft.profiles) ? draft.profiles : null;
  if (!profiles) issues.push(issue('profiles', t('config_err_required')));
  else Object.entries(profiles).forEach(([id, profile]) => {
    if (!id) issues.push(issue('profiles.' + id + '.id', t('config_err_required')));
    if (!isPlainObject(profile)) {
      issues.push(issue('profiles.' + id, t('config_err_required')));
      return;
    }
    PROFILE_FIELDS.forEach(field => {
      if (!Object.prototype.hasOwnProperty.call(profile, field)) issues.push(issue('profiles.' + id + '.' + field, t('config_err_required')));
    });
    Object.keys(profile).forEach(field => {
      if (!PROFILE_FIELDS.includes(field)) issues.push(issue('profiles.' + id + '.' + field, t('config_err_unknown')));
    });
    ['name', 'description', 'shortProfileDescription'].forEach(field => {
      const pair = profile[field];
      if (!Array.isArray(pair) || pair.length !== 2 || pair.some(item => typeof item !== 'string')) {
        issues.push(issue('profiles.' + id + '.' + field, t('config_err_required')));
      }
    });
    if (typeof profile.flag !== 'string') issues.push(issue('profiles.' + id + '.flag', t('config_err_required')));
    if (typeof profile.masterLink !== 'string') issues.push(issue('profiles.' + id + '.masterLink', t('config_err_required')));
    if (typeof profile.whitelist !== 'boolean') issues.push(issue('profiles.' + id + '.whitelist', t('config_err_required')));
    ['json', 'xhttpExtra'].forEach(field => {
      if (typeof profile[field] === 'string' || (profile[field] !== undefined && !isPlainObject(profile[field]))) {
        issues.push(issue('profiles.' + id + '.' + field, t('config_err_json')));
      }
    });
    if (typeof profile.node !== 'string' || !Object.prototype.hasOwnProperty.call(nodes, profile.node)) {
      issues.push(issue('profiles.' + id + '.node', t('config_err_node')));
      issues.push(issue('nodes', t('config_err_node')));
    }
  });

  const panels = isPlainObject(draft['3xui']) ? draft['3xui'] : null;
  if (!panels || !Object.keys(panels).length) issues.push(issue('3xui', t('config_last_panel')));
  else Object.entries(panels).forEach(([id, panel]) => {
    if (!id) issues.push(issue('3xui.' + id + '.id', t('config_err_required')));
    if (!isPlainObject(panel)) {
      issues.push(issue('3xui.' + id, t('config_err_required')));
      return;
    }
    PANEL_FIELDS.forEach(field => {
      if (!Object.prototype.hasOwnProperty.call(panel, field)) issues.push(issue('3xui.' + id + '.' + field, t('config_err_required')));
    });
    Object.keys(panel).forEach(field => {
      if (!PANEL_FIELDS.includes(field) && !PANEL_OPTIONAL.includes(field)) issues.push(issue('3xui.' + id + '.' + field, t('config_err_unknown')));
    });
    ['name', 'address', 'uri'].forEach(field => {
      if (typeof panel[field] !== 'string') issues.push(issue('3xui.' + id + '.' + field, t('config_err_required')));
    });
    if (typeof panel.port !== 'string' && !isInteger(panel.port)) issues.push(issue('3xui.' + id + '.port', t('config_err_integer')));
    if (typeof panel.token !== 'string' || Array.from(panel.token).length < 20) issues.push(issue('3xui.' + id + '.token', t('config_err_token_len')));
    if (typeof panel.https !== 'boolean') issues.push(issue('3xui.' + id + '.https', t('config_err_required')));
    if (typeof panel.whitelist !== 'boolean') issues.push(issue('3xui.' + id + '.whitelist', t('config_err_required')));
    if (panel.mode !== 'whitelist' && panel.mode !== 'blacklist') issues.push(issue('3xui.' + id + '.mode', t('config_err_required')));
    if (!Array.isArray(panel.inbounds_list) || panel.inbounds_list.some(item => !isInteger(item))) {
      issues.push(issue('3xui.' + id + '.inbounds_list', t('config_err_integer')));
    }
    if (Object.prototype.hasOwnProperty.call(panel, 'nginx_auth')) {
      const pair = panel.nginx_auth;
      if (!Array.isArray(pair) || pair.length !== 2 || pair.some(item => typeof item !== 'string')) {
        issues.push(issue('3xui.' + id + '.nginx_auth', t('config_err_required')));
      }
    }
    if (Object.prototype.hasOwnProperty.call(panel, 'inject_headers')) {
      const headers = panel.inject_headers;
      if (!isPlainObject(headers) || Object.values(headers).some(item => typeof item !== 'string')) {
        issues.push(issue('3xui.' + id + '.inject_headers', t('config_err_required')));
      }
    }
  });
  return issues;
}

function fieldOf(path) {
  if (path.startsWith('profiles.') || path.startsWith('3xui.')) return path;
  return path.split('.')[0];
}

function sectionOf(path) {
  if (path.startsWith('profiles')) return 'inbounds';
  if (path.startsWith('3xui')) return 'panels';
  return 'main';
}

function issueParts(path) {
  const section = sectionOf(path);
  if (section === 'main') return [path];
  const root = section === 'inbounds' ? 'profiles' : '3xui';
  const map = editor.draft[root] || {};
  const id = Object.keys(map).sort((a, b) => b.length - a.length)
    .find(key => path === root + '.' + key || path.startsWith(root + '.' + key + '.'));
  return id === undefined ? [root] : [root, id, path.slice(root.length + id.length + 2) || 'id'];
}

function showIssues(issues) {
  document.querySelectorAll('.config-error').forEach(el => { el.hidden = true; el.textContent = ''; });
  issues.forEach(item => {
    const parts = issueParts(item.path);
    let id = '';
    if (parts[0] === 'profiles' && parts[1]) id = 'cfg-in-err-' + cssId(parts[1]) + '-' + (parts[2] || 'id');
    else if (parts[0] === '3xui' && parts[1]) id = 'cfg-pn-err-' + cssId(parts[1]) + '-' + (parts[2] || 'id');
    else id = 'cfg-err-' + parts[0];
    const el = document.getElementById(id);
    if (el && el.hidden) {
      el.hidden = false;
      el.textContent = item.message;
    }
  });
}

function validateField(key) {
  const issues = validateDraft(editor.draft).filter(item => fieldOf(item.path) === key);
  if (key === 'nodes') {
    const keys = readRows('cfg-rows-nodes', 'pair').map(row => row[0]);
    if (new Set(keys).size !== keys.length) issues.unshift(issue('nodes', t('config_err_unique')));
  }
  const el = document.getElementById('cfg-err-' + key.replace('$', ''));
  if (el) {
    el.hidden = !issues.length;
    el.textContent = issues[0]?.message || '';
  }
}

function validateCard(kind, id) {
  const prefix = kind === 'inbounds' ? 'profiles.' + id : '3xui.' + id;
  const issues = validateDraft(editor.draft).filter(item => item.path === prefix || item.path.startsWith(prefix + '.'));
  const card = cardRoot(kind, id);
  const rawId = card?.querySelector('[data-field="id"]')?.value.trim();
  const map = kind === 'inbounds' ? editor.draft.profiles : editor.draft['3xui'];
  if (rawId !== undefined && (!rawId || (rawId !== id && Object.hasOwn(map, rawId)))) {
    issues.unshift(issue(prefix + '.id', t(rawId ? 'config_err_unique' : 'config_err_required')));
  }
  if (kind === 'panels') {
    const headers = readRows('cfg-pn-headers-' + cssId(id), 'pair').map(row => row[0]);
    if (new Set(headers).size !== headers.length) issues.push(issue(prefix + '.inject_headers', t('config_err_unique')));
  }
  card?.querySelectorAll('.config-error').forEach(el => { el.hidden = true; el.textContent = ''; });
  issues.forEach(item => {
    const field = issueParts(item.path)[2] || 'id';
    const el = document.getElementById((kind === 'inbounds' ? 'cfg-in-err-' : 'cfg-pn-err-') + cssId(id) + '-' + field);
    if (el && el.hidden) {
      el.hidden = false;
      el.textContent = item.message;
    }
  });
}

function validateAll() {
  const issues = validateDraft(editor.draft);
  const nodeKeys = readRows('cfg-rows-nodes', 'pair').map(row => row[0]);
  if (new Set(nodeKeys).size !== nodeKeys.length) issues.unshift(issue('nodes', t('config_err_unique')));
  for (const kind of ['inbounds', 'panels']) {
    const root = kind === 'inbounds' ? 'profiles' : '3xui';
    Object.keys(editor.draft[root] || {}).forEach(id => {
      const raw = cardRoot(kind, id)?.querySelector('[data-field="id"]')?.value.trim();
      if (raw !== undefined && raw !== id) issues.push(issue(root + '.' + id + '.id', t(raw ? 'config_err_unique' : 'config_err_required')));
      if (kind === 'panels') {
        const headers = readRows('cfg-pn-headers-' + cssId(id), 'pair').map(row => row[0]);
        if (new Set(headers).size !== headers.length) issues.push(issue(root + '.' + id + '.inject_headers', t('config_err_unique')));
      }
    });
  }
  showIssues(issues);
  return issues;
}

function firstInvalid(issues) {
  return issues[0] || null;
}

function focusIssue(item) {
  if (!item) return;
  const section = sectionOf(item.path);
  showConfigSection(section);
  const parts = issueParts(item.path);
  if (parts[1]) {
    editor.open[section].add(parts[1]);
    const card = cardRoot(section, parts[1]);
    card?.classList.add('open');
    card?.querySelector('.config-card-toggle').setAttribute('aria-expanded', 'true');
  }
  const target = document.getElementById(
    parts[0] === 'profiles' ? 'cfg-in-err-' + cssId(parts[1] || '') + '-' + (parts[2] || 'id')
      : parts[0] === '3xui' ? 'cfg-pn-err-' + cssId(parts[1] || '') + '-' + (parts[2] || 'id')
        : 'cfg-err-' + parts[0]
  );
  (target?.previousElementSibling || target)?.scrollIntoView({ block: 'center' });
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
        focusIssue(firstInvalid(issues));
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

export function showConfigSection(name) {
  document.querySelectorAll('.config-tab').forEach(el => el.classList.toggle('active', el.dataset.section === name));
  document.querySelectorAll('.config-pane').forEach(el => el.classList.toggle('active', el.id === 'config-pane-' + name));
}

export function configOnTab() {
  const active = document.getElementById('tab-config')?.classList.contains('active');
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

function openNew(kind) {
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
  body.querySelector('input, textarea, select')?.focus();
  const close = (created) => {
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

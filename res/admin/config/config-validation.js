import { refreshJsonEditors } from '../json-editor.js';
import { editor, KNOWN_KEYS, REQUIRED_KEYS, TEMPLATE_KEYS, PROFILE_FIELDS, PANEL_FIELDS, PANEL_OPTIONAL } from './config-state.js';
import { isPlainObject, isInteger, isFiniteNumber, jsonEqual } from './config-json.js';
import { cardRoot, cssId, readRows, showConfigSection, enhanceJsonInputs } from './config-controls.js';

function issue(path, message) {
  return { path, message };
}

export function validateDraft(draft) {
  const issues = [];
  ['uri', 'api_uri', 'fallback_domain', 'domain', 'ping_check_url', 'provider_id', 'sub_name', 'api_token', 'salt', '$schema'].forEach(key => {
    if (Object.hasOwn(draft, key) && typeof draft[key] !== 'string') issues.push(issue(key, t('config_err_required')));
  });
  ['redis', 'bot', 'publicbot', 'support_text'].forEach(key => {
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
  if (isPlainObject(draft.support_text)) {
    Object.entries(draft.support_text).forEach(([platform, pair]) => {
      if (!['discord', 'telegram'].includes(platform)) issues.push(issue('support_text', t('config_err_unknown')));
      else if (!Array.isArray(pair) || pair.length !== 2 || pair.some(item => typeof item !== 'string')) {
        issues.push(issue('support_text', t('config_err_required')));
      }
    });
  }
  if (typeof draft.json_template === 'string') issues.push(issue('json_template', t('config_err_json')));
  else if (isPlainObject(draft.json_template)) {
    const missing = TEMPLATE_KEYS.filter(key => !Object.hasOwn(draft.json_template, key));
    const errors = [];
    ['routing', 'servers'].forEach(key => {
      if (!Object.hasOwn(draft.json_template, key)) return;
      if (!isPlainObject(draft.json_template[key])) {
        errors.push(t('config_err_object_field').replace('{field}', key));
        return;
      }
      const required = key === 'routing' ? ['rules', 'domainStrategy'] : ['servers', 'queryStrategy'];
      required.forEach(field => {
        if (!Object.hasOwn(draft.json_template[key], field)) missing.push(key + '.' + field);
      });
    });
    const servers = draft.json_template.servers;
    if (isPlainObject(servers)) {
      if (Object.hasOwn(servers, 'servers') && !Array.isArray(servers.servers)) {
        errors.push(t('config_err_array_field').replace('{field}', 'servers.servers'));
      }
      if (Object.hasOwn(servers, 'queryStrategy') && typeof servers.queryStrategy !== 'string') {
        errors.push(t('config_err_string_field').replace('{field}', 'servers.queryStrategy'));
      }
      Object.keys(servers).filter(key => !['servers', 'queryStrategy'].includes(key)).forEach(key => {
        errors.push(t('config_err_unknown') + ': servers.' + key);
      });
    }
    if (missing.length) errors.unshift(t('config_err_missing_fields').replace('{fields}', missing.join(', ')));
    if (errors.length) issues.push(issue('json_template', errors.join('; ')));
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
  if (!panels) issues.push(issue('3xui', t('config_err_required')));
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

export function validateField(key) {
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

export function validateCard(kind, id) {
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

export function validateAll() {
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

export function focusIssue(item) {
  if (!item) return;
  const section = sectionOf(item.path);
  showConfigSection(section);
  const parts = issueParts(item.path);
  if (parts[1]) {
    editor.open[section].add(parts[1]);
    const card = cardRoot(section, parts[1]);
    card?.classList.add('open');
    if (card && section === 'inbounds') enhanceJsonInputs(card);
    if (card) refreshJsonEditors(card);
    card?.querySelector('.config-card-toggle').setAttribute('aria-expanded', 'true');
  }
  const target = document.getElementById(
    parts[0] === 'profiles' ? 'cfg-in-err-' + cssId(parts[1] || '') + '-' + (parts[2] || 'id')
      : parts[0] === '3xui' ? 'cfg-pn-err-' + cssId(parts[1] || '') + '-' + (parts[2] || 'id')
        : 'cfg-err-' + parts[0]
  );
  (target?.previousElementSibling || target)?.scrollIntoView({ block: 'center' });
}

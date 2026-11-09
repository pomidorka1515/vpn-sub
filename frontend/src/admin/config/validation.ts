import type { Translate } from '../../shared/formatting';
import { refreshJsonEditors } from './editor-bridge';
import { editor, KNOWN_KEYS, REQUIRED_KEYS, TEMPLATE_KEYS, PROFILE_FIELDS, PANEL_FIELDS, PANEL_OPTIONAL } from './state';
import { ConfigNumber, isPlainObject, isInteger, isFiniteNumber, jsonEqual } from './json';
import type { ConfigObject } from './json';
import { cardRoot, cssId, readRows, showConfigSection, enhanceJsonInputs } from './controls';

export interface ValidationIssue { path: string; message: string }
let t: Translate = key => key;
export function initConfigValidation(translate: Translate): () => void { t = translate; return () => { t = key => key; }; }
const currentDraft = (): ConfigObject => editor.draft ?? {};

function issue(path: string, message: string): ValidationIssue {
  return { path, message };
}

const TELEGRAM_TOKEN_RE = /^[1-9][0-9]*:[A-Za-z0-9_-]{35}$/;
const DISCORD_TOKEN_RE = /^[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,38}$/;

function validHttpUrl(value: string, paths: boolean): boolean {
  // Check the raw input too: URL parsing silently normalizes whitespace and backslashes.
  if (/[\s\\]/.test(value) || !/^https?:\/\//.test(value)) return false;
  const authority = value.slice(value.indexOf('://') + 3).split(/[/?#]/)[0] ?? '';
  if (!paths && !/^https?:\/\/[^/?#]+\/?$/.test(value)) return false;
  if (!/^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?::[0-9]{1,5})?$/.test(authority)) return false;
  try {
    const url = new URL(value);
    return url.hostname.length <= 253 && !url.username && !url.password;
  } catch { return false; }
}

function validSnowflake(value: ConfigObject[string]): boolean {
  if (!isInteger(value) || (typeof value === 'number' && !Number.isSafeInteger(value))) return false;
  if (typeof value !== 'number' && !(value instanceof ConfigNumber)) return false;
  const raw = String(value);
  return /^[1-9][0-9]{0,19}$/.test(raw) && BigInt(raw) <= 18446744073709551615n;
}

function validPanelAddress(value: string): boolean {
  if (!value || /[\s\\/?#@%]/.test(value)) return false;
  if (value.includes(':')) {
    const host = value.startsWith('[') && value.endsWith(']') ? value : '[' + value + ']';
    try { return new URL('http://' + host).hostname.startsWith('['); } catch { return false; }
  }
  if (/^[0-9.]+$/.test(value)) {
    return value.split('.').length === 4 && value.split('.').every(part => /^(?:0|[1-9][0-9]{0,2})$/.test(part) && Number(part) <= 255);
  }
  const domain = value.endsWith('.') ? value.slice(0, -1) : value;
  return domain.length <= 253 && domain.split('.').every(part => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(part));
}

function validServicePath(value: string): boolean {
  // Root and relative prefixes are supported; leading/trailing slashes are optional.
  if (!/^(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/]|%[0-9A-Fa-f]{2})*$/.test(value) || value.includes('//') || /^[^/]*:/.test(value)) return false;
  try {
    return value.split('/').every(part => {
      const decoded = decodeURIComponent(part);
      return decoded !== '.' && decoded !== '..' && !/[\s\\/\u0000-\u001f\u007f]/.test(decoded);
    });
  } catch { return false; }
}

export function validateDraft(draft: ConfigObject): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  ['uri', 'api_uri', 'fallback_domain', 'domain', 'ping_check_url', 'provider_id', 'sub_name', 'api_token', 'salt', '$schema'].forEach(key => {
    if (Object.hasOwn(draft, key) && typeof draft[key] !== 'string') issues.push(issue(key, t('config_err_required')));
  });
  ['uri', 'api_uri'].forEach(key => {
    const value = draft[key];
    if (typeof value === 'string' && !validServicePath(value)) issues.push(issue(key, t('config_err_uri_path')));
  });
  ['domain', 'fallback_domain', 'ping_check_url'].forEach(key => {
    const value = draft[key];
    if (typeof value === 'string' && !validHttpUrl(value, key === 'ping_check_url')) {
      issues.push(issue(key, t(key === 'ping_check_url' ? 'config_err_http_url' : 'config_err_domain')));
    }
  });
  ['redis', 'bot', 'publicbot', 'discord', 'support_text'].forEach(key => {
    if (Object.hasOwn(draft, key) && !isPlainObject(draft[key])) issues.push(issue(key, t('config_err_required')));
  });
  if (!Array.isArray(draft['api_admin_ui_auth'])) issues.push(issue('api_admin_ui_auth', t('config_err_required')));
  const keys = Object.keys(draft);
  keys.forEach(key => {
    if (!KNOWN_KEYS.includes(key)) issues.push(issue(key, t('config_err_unknown')));
  });
  REQUIRED_KEYS.forEach(key => {
    // Browser config reads omit the independent machine credential (write-only).
    if (key === 'api_token') return;
    if (!Object.prototype.hasOwnProperty.call(draft, key)) issues.push(issue(key, t('config_err_required')));
  });
  if (editor.loaded && Object.prototype.hasOwnProperty.call(draft, '$schema') && !jsonEqual(draft['$schema'], editor.loaded['$schema'])) {
    issues.push(issue('$schema', t('config_schema_locked')));
  }
  if (typeof draft['api_token'] === 'string' && draft['api_token'] === '') issues.push(issue('api_token', t('config_err_required')));
  const auth = draft['api_admin_ui_auth'];
  if (Array.isArray(auth) && (auth.length !== 2 || auth.some(item => typeof item !== 'string' || item === ''))) {
    issues.push(issue('api_admin_ui_auth', t('config_err_required')));
  }
  if (Object.prototype.hasOwnProperty.call(draft, 'panel_alert_cooldown') && (!isInteger(draft['panel_alert_cooldown']) || Number(draft['panel_alert_cooldown']) < 0)) {
    issues.push(issue('panel_alert_cooldown', t('config_err_positive_integer')));
  }
  ['fingerprints', 'bypass_packages'].forEach(key => {
    const list = draft[key];
    if (!Array.isArray(list) || list.some(item => typeof item !== 'string')) issues.push(issue(key, t('config_err_required')));
    else if (key === 'bypass_packages' && list.some(item => typeof item !== 'string' || !/^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$/.test(item) || /\s/.test(item))) issues.push(issue(key, t('config_err_package_name')));
    else if (key === 'fingerprints' && (list.length === 0 || list.some(item => typeof item !== 'string' || !['chrome', 'safari', 'edge', 'ios', 'firefox', 'android', '360', 'qq', 'random', 'randomized'].includes(item)))) issues.push(issue(key, t('config_err_fingerprints')));
    else if (key === 'fingerprints' && new Set(list).size !== list.length) issues.push(issue(key, t('config_err_unique')));
  });
  if (Object.prototype.hasOwnProperty.call(draft, 'funny_strings')) {
    const list = draft['funny_strings'];
    if (!Array.isArray(list) || list.some(item => typeof item !== 'string')) issues.push(issue('funny_strings', t('config_err_required')));
  }
  if (!isPlainObject(draft['nodes']) || Object.entries(draft['nodes']).some(([key, value]) => !key || typeof value !== 'string')) {
    issues.push(issue('nodes', t('config_err_required')));
  }
  if (isPlainObject(draft['redis'])) {
    const extra = Object.keys(draft['redis']).filter(key => key !== 'url' && key !== 'socket_timeout');
    if (typeof draft['redis']['url'] !== 'string' || draft['redis']['url'] === '') issues.push(issue('redis', t('config_err_required')));
    else if (!draft['redis']['url'].startsWith('redis://')) issues.push(issue('redis', t('config_err_redis_url')));
    if (Object.prototype.hasOwnProperty.call(draft['redis'], 'socket_timeout') && !(isFiniteNumber(draft['redis']['socket_timeout']) && Number(draft['redis']['socket_timeout']) > 0)) {
      issues.push(issue('redis', t('config_err_integer')));
    }
    if (extra.length) issues.push(issue('redis', t('config_err_unknown')));
  }
  ['bot', 'discord'].forEach(key => {
    const bot = draft[key];
    if (!isPlainObject(bot)) return;
    const extra = Object.keys(bot).filter(field => field !== 'token' && field !== 'whitelist');
    if (typeof bot['token'] !== 'string') issues.push(issue(key, t('config_err_required')));
    else if (bot['token'] !== '' && (/\s/.test(bot['token']) || !(key === 'discord' ? DISCORD_TOKEN_RE : TELEGRAM_TOKEN_RE).test(bot['token']))) issues.push(issue(key, t(key === 'discord' ? 'config_err_discord_token' : 'config_err_telegram_token')));
    if (!Array.isArray(bot['whitelist']) || bot['whitelist'].some(item => key === 'discord' ? !validSnowflake(item) : !isInteger(item) || Number(item) < 0)) issues.push(issue(key, t(key === 'discord' ? 'config_err_snowflake' : 'config_err_positive_integer')));
    if (key === 'discord' && Array.isArray(bot['whitelist']) && new Set(bot['whitelist'].map(String)).size !== bot['whitelist'].length) issues.push(issue(key, t('config_err_unique')));
    if (extra.length) issues.push(issue(key, t('config_err_unknown')));
  });
  if (Object.prototype.hasOwnProperty.call(draft, 'publicbot') && isPlainObject(draft['publicbot'])) {
    if (typeof draft['publicbot']['token'] !== 'string' || Object.keys(draft['publicbot']).some(key => key !== 'token')) {
      issues.push(issue('publicbot', t('config_err_required')));
    }
    else if (draft['publicbot']['token'] !== '' && (/\s/.test(draft['publicbot']['token']) || !TELEGRAM_TOKEN_RE.test(draft['publicbot']['token']))) issues.push(issue('publicbot', t('config_err_telegram_token')));
  }
  if (isPlainObject(draft['support_text'])) {
    Object.entries(draft['support_text']).forEach(([platform, pair]) => {
      if (!['discord', 'telegram'].includes(platform)) issues.push(issue('support_text', t('config_err_unknown')));
      else if (!Array.isArray(pair) || pair.length !== 2 || pair.some(item => typeof item !== 'string')) {
        issues.push(issue('support_text', t('config_err_required')));
      }
    });
  }
  if (typeof draft['json_template'] === 'string') issues.push(issue('json_template', t('config_err_json')));
  else if (isPlainObject(draft['json_template'])) {
    const template = draft['json_template'];
    const missing = TEMPLATE_KEYS.filter(key => !Object.hasOwn(template, key));
    const errors: string[] = [];
    ['routing', 'servers'].forEach(key => {
      if (!Object.hasOwn(template, key)) return;
      const part = template[key];
      if (!isPlainObject(part)) {
        errors.push(t('config_err_object_field').replace('{field}', key));
        return;
      }
      const required = key === 'routing' ? ['rules', 'domainStrategy'] : ['servers', 'queryStrategy'];
      required.forEach(field => {
        if (!Object.hasOwn(part, field)) missing.push(key + '.' + field);
      });
    });
    const servers = template['servers'];
    if (isPlainObject(servers)) {
      if (Object.hasOwn(servers, 'servers') && !Array.isArray(servers['servers'])) {
        errors.push(t('config_err_array_field').replace('{field}', 'servers.servers'));
      }
      if (Object.hasOwn(servers, 'queryStrategy') && typeof servers['queryStrategy'] !== 'string') {
        errors.push(t('config_err_string_field').replace('{field}', 'servers.queryStrategy'));
      }
      Object.keys(servers).filter(key => !['servers', 'queryStrategy'].includes(key)).forEach(key => {
        errors.push(t('config_err_unknown') + ': servers.' + key);
      });
    }
    if (missing.length) errors.unshift(t('config_err_missing_fields').replace('{fields}', missing.join(', ')));
    if (errors.length) issues.push(issue('json_template', errors.join('; ')));
  } else if (draft['json_template'] !== undefined) issues.push(issue('json_template', t('config_err_json')));

  const nodes = isPlainObject(draft['nodes']) ? draft['nodes'] : {};
  const profiles = isPlainObject(draft['profiles']) ? draft['profiles'] : null;
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
    if (typeof profile['flag'] !== 'string') issues.push(issue('profiles.' + id + '.flag', t('config_err_required')));
    if (typeof profile['masterLink'] !== 'string') issues.push(issue('profiles.' + id + '.masterLink', t('config_err_required')));
    if (typeof profile['whitelist'] !== 'boolean') issues.push(issue('profiles.' + id + '.whitelist', t('config_err_required')));
    ['json', 'xhttpExtra'].forEach(field => {
      if (typeof profile[field] === 'string' || (profile[field] !== undefined && !isPlainObject(profile[field]))) {
        issues.push(issue('profiles.' + id + '.' + field, t('config_err_json')));
      }
    });
    if (typeof profile['node'] !== 'string' || !Object.prototype.hasOwnProperty.call(nodes, profile['node'])) {
      issues.push(issue('profiles.' + id + '.node', t('config_err_node')));
      issues.push(issue('nodes', t('config_err_node')));
    }
  });

  const panels = isPlainObject(draft['3xui']) ? draft['3xui'] : null;
  const whitelistPanels = panels ? Object.entries(panels).filter(([, panel]) => isPlainObject(panel) && panel['whitelist'] === true) : [];
  if (whitelistPanels.length > 1) whitelistPanels.forEach(([id]) => issues.push(issue('3xui.' + id + '.whitelist', t('config_err_whitelist_panel_limit'))));
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
    if (typeof panel['address'] === 'string' && !validPanelAddress(panel['address'])) issues.push(issue('3xui.' + id + '.address', t('config_err_panel_address')));
    if (typeof panel['uri'] === 'string' && !validServicePath(panel['uri'])) issues.push(issue('3xui.' + id + '.uri', t('config_err_panel_path')));
    if (!(typeof panel['port'] === 'string' ? /^[0-9]+$/.test(panel['port']) : isInteger(panel['port'])) || Number(panel['port']) < 0 || Number(panel['port']) > 65535) issues.push(issue('3xui.' + id + '.port', t('config_err_panel_port')));
    if (typeof panel['token'] !== 'string' || Array.from(panel['token']).length < 20) issues.push(issue('3xui.' + id + '.token', t('config_err_token_len')));
    if (typeof panel['https'] !== 'boolean') issues.push(issue('3xui.' + id + '.https', t('config_err_required')));
    if (typeof panel['whitelist'] !== 'boolean') issues.push(issue('3xui.' + id + '.whitelist', t('config_err_required')));
    if (panel['mode'] !== 'whitelist' && panel['mode'] !== 'blacklist') issues.push(issue('3xui.' + id + '.mode', t('config_err_required')));
    if (!Array.isArray(panel['inbounds_list']) || panel['inbounds_list'].some(item => !isInteger(item) || Number(item) < 0)) {
      issues.push(issue('3xui.' + id + '.inbounds_list', t('config_err_positive_integer')));
    }
    if (Object.prototype.hasOwnProperty.call(panel, 'inject_headers')) {
      const headers = panel['inject_headers'];
      if (!isPlainObject(headers) || Object.values(headers).some(item => typeof item !== 'string')) {
        issues.push(issue('3xui.' + id + '.inject_headers', t('config_err_required')));
      }
    }
  });
  return issues;
}

function fieldOf(path: string): string {
  if (path.startsWith('profiles.') || path.startsWith('3xui.')) return path;
  return path.split('.')[0] ?? '';
}

function sectionOf(path: string): 'inbounds' | 'panels' | 'main' {
  if (path.startsWith('profiles')) return 'inbounds';
  if (path.startsWith('3xui')) return 'panels';
  return 'main';
}

function issueParts(path: string): [string, ...string[]] {
  const section = sectionOf(path);
  if (section === 'main') return [path];
  const root = section === 'inbounds' ? 'profiles' : '3xui';
  const value = currentDraft()[root];
  const map = isPlainObject(value) ? value : {};
  const id = Object.keys(map).sort((a, b) => b.length - a.length)
    .find(key => path === root + '.' + key || path.startsWith(root + '.' + key + '.'));
  return id === undefined ? [root] : [root, id, path.slice(root.length + id.length + 2) || 'id'];
}

function showIssues(issues: ValidationIssue[]): void {
  document.querySelectorAll<HTMLElement>('.config-error').forEach(el => { el.hidden = true; el.textContent = ''; });
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

export function validateField(key: string): void {
  const issues = validateDraft(currentDraft()).filter(item => fieldOf(item.path) === key);
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

export function validateCard(kind: string, id: string): void {
  const prefix = kind === 'inbounds' ? 'profiles.' + id : '3xui.' + id;
  const issues = validateDraft(currentDraft()).filter(item => item.path === prefix || item.path.startsWith(prefix + '.'));
  const card = cardRoot(kind, id);
  const rawId = card?.querySelector<HTMLInputElement>('[data-field="id"]')?.value.trim();
  const value = kind === 'inbounds' ? currentDraft()['profiles'] : currentDraft()['3xui'];
  const map = isPlainObject(value) ? value : {};
  if (rawId !== undefined && (!rawId || (rawId !== id && Object.hasOwn(map, rawId)))) {
    issues.unshift(issue(prefix + '.id', t(rawId ? 'config_err_unique' : 'config_err_required')));
  }
  if (kind === 'panels') {
    const headers = readRows('cfg-pn-headers-' + cssId(id), 'pair').map(row => row[0]);
    if (new Set(headers).size !== headers.length) issues.push(issue(prefix + '.inject_headers', t('config_err_unique')));
  }
  card?.querySelectorAll<HTMLElement>('.config-error').forEach(el => { el.hidden = true; el.textContent = ''; });
  issues.forEach(item => {
    const field = issueParts(item.path)[2] || 'id';
    const el = document.getElementById((kind === 'inbounds' ? 'cfg-in-err-' : 'cfg-pn-err-') + cssId(id) + '-' + field);
    if (el && el.hidden) {
      el.hidden = false;
      el.textContent = item.message;
    }
  });
}

export function validateAll(): ValidationIssue[] {
  const issues = validateDraft(currentDraft());
  const nodeKeys = readRows('cfg-rows-nodes', 'pair').map(row => row[0]);
  if (new Set(nodeKeys).size !== nodeKeys.length) issues.unshift(issue('nodes', t('config_err_unique')));
  for (const kind of ['inbounds', 'panels']) {
    const root = kind === 'inbounds' ? 'profiles' : '3xui';
    const value = currentDraft()[root];
    Object.keys(isPlainObject(value) ? value : {}).forEach(id => {
      const raw = cardRoot(kind, id)?.querySelector<HTMLInputElement>('[data-field="id"]')?.value.trim();
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

export function focusIssue(item: ValidationIssue | undefined): void {
  if (!item) return;
  const section = sectionOf(item.path);
  showConfigSection(section);
  const parts = issueParts(item.path);
  if (parts[1] && section !== 'main') {
    editor.open[section].add(parts[1]);
    const card = cardRoot(section, parts[1]);
    card?.classList.add('open');
    if (card && section === 'inbounds') enhanceJsonInputs(card);
    if (card) refreshJsonEditors(card);
    card?.querySelector('.config-card-toggle')?.setAttribute('aria-expanded', 'true');
  }
  const target = document.getElementById(
    parts[0] === 'profiles' ? 'cfg-in-err-' + cssId(parts[1] || '') + '-' + (parts[2] || 'id')
      : parts[0] === '3xui' ? 'cfg-pn-err-' + cssId(parts[1] || '') + '-' + (parts[2] || 'id')
        : 'cfg-err-' + parts[0]
  );
  (target?.previousElementSibling || target)?.scrollIntoView({ block: 'center' });
}

import { destroyJsonEditors } from '../json-editor.js';
import { editor, INTEGER_RE, OPTIONAL_KEYS, RESTART_KEYS, MAIN_SECTIONS } from './config-state.js';
import { ConfigNumber, parseConfigJson, isPlainObject, jsonText } from './config-json.js';
import { attr, restartIcon, fieldError, passWrap, readText, enhanceJsonInputs, rowList, bindRows, nodeOptions } from './config-controls.js';
import { validateField, validateCard } from './config-validation.js';

export function createMainEditor(markDirty) {
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
    const expanded = slot.querySelector('.config-box-collapse')?.open;
    destroyJsonEditors(slot);
    slot.outerHTML = boxHtml(key);
    if (expanded) document.getElementById('cfg-box-' + key.replace('$', ''))?.querySelector('.config-box-collapse')?.setAttribute('open', '');
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
    } else if (key === 'support_text') {
      const obj = isPlainObject(editor.draft.support_text) ? editor.draft.support_text : {};
      control = ['discord', 'telegram'].map(platform => {
        const enabled = Object.hasOwn(obj, platform);
        const pair = Array.isArray(obj[platform]) ? obj[platform] : ['', ''];
        return `<label class="config-sub">${platform}</label>
          ${enabled ? ['en', 'ru'].map((lang, index) => `<label for="cfg-support-${platform}-${index}">${escapeHtml(t('config_' + lang))}</label>
            <textarea id="cfg-support-${platform}-${index}" spellcheck="false">${escapeHtml(pair[index] ?? '')}</textarea>`).join('') : `<div class="config-absent">${escapeHtml(t('config_not_set'))}</div>`}
          <button type="button" class="btn-tiny" data-support-platform="${platform}">${escapeHtml(t(enabled ? 'config_remove' : 'config_add'))}</button>`;
      }).join('');
    } else if (key === 'json_template') {
      const value = editor.jsonInputs.get('json_template') ?? jsonText(editor.draft.json_template);
      control = `<textarea id="cfg-json_template" class="config-json" spellcheck="false">${escapeHtml(value)}</textarea>`;
    } else if (key === '$schema') {
      control = `<div class="config-locked">${escapeHtml(editor.draft.$schema ?? '')}</div>`;
    }
    const remove = optional && present && key !== 'bot' && key !== 'publicbot'
      ? `<button type="button" class="btn-tiny danger" data-optional-remove="${key}">${escapeHtml(t('config_remove'))}</button>`
      : '';
    if (key === 'bypass_packages') {
      return `<div class="config-box" id="cfg-box-${safeKey}"><details class="config-box-collapse">
        <summary class="config-box-label">${escapeHtml(key)}</summary>
        <div class="config-box-content">${hint}${control}</div>
      </details>${fieldError('cfg-err-' + safeKey)}</div>`;
    }
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
    if (key === 'support_text') {
      slot.querySelectorAll('[data-support-platform]').forEach(button => {
        button.addEventListener('click', () => {
          const platform = button.dataset.supportPlatform;
          if (Object.hasOwn(editor.draft.support_text, platform)) delete editor.draft.support_text[platform];
          else editor.draft.support_text[platform] = ['', ''];
          paintBox(key);
          validateField(key);
          markDirty();
        });
      });
      ['discord', 'telegram'].forEach(platform => {
        [0, 1].forEach(index => {
          document.getElementById(`cfg-support-${platform}-${index}`)?.addEventListener('input', event => {
            editor.draft.support_text[platform][index] = event.target.value;
            validateField(key);
            markDirty();
          });
        });
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
      enhanceJsonInputs(slot);
    }
  }

  function addOptional(key) {
    if (key === 'api_uri' || key === 'fallback_domain') editor.draft[key] = '';
    else if (key === 'funny_strings') editor.draft[key] = [];
    else if (key === 'bot') editor.draft.bot = { token: '', whitelist: [] };
    else if (key === 'publicbot') editor.draft.publicbot = { token: '' };
    else if (key === 'support_text') editor.draft.support_text = {};
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
    destroyJsonEditors(pane);
    pane.innerHTML = MAIN_SECTIONS.map(([title, keys]) => `
      <div class="section-title">${escapeHtml(t(title))}</div>
      ${keys.map(boxHtml).join('')}
    `).join('');
    MAIN_SECTIONS.forEach(([, keys]) => keys.forEach(bindBox));
  }

  function refreshNodeSelects() {
    document.querySelectorAll('.cfg-node-select').forEach(select => {
      const current = select.value;
      select.innerHTML = nodeOptions(current);
    });
    Object.keys(editor.draft.profiles || {}).forEach(id => validateCard('inbounds', id));
    validateField('nodes');
  }

  return { renderMain };
}

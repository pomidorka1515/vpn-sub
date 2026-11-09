import { required, requiredInstance } from './required';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMainEditor } from '../admin/config/main';
import { createCardEditor } from '../admin/config/cards';
import { ConfigNumber, stringifyConfig } from '../admin/config/json';
import type { ConfigValue } from '../admin/config/json';
import { editor, OPTIONAL_KEYS, RESTART_KEYS } from '../admin/config/state';
import { validateDraft } from '../admin/config/validation';

afterEach(() => {
  editor.draft = null;
  editor.loaded = null;
  editor.jsonInputs.clear();
});

const discordIssues = (discord: ConfigValue) => validateDraft({ discord }).filter(issue => issue.path === 'discord');
const discordToken = 'A'.repeat(26) + '.' + 'B'.repeat(6) + '.' + 'C'.repeat(38);

it('describes Master Link placeholders beside the inbound field', () => {
  document.body.innerHTML = '<div id="tab-config"><div id="config-pane-inbounds"></div></div>';
  editor.draft = { profiles: { local: {} } };
  createCardEditor({ markDirty: vi.fn(), openNew: vi.fn(), t: key => key, language: () => 'en', confirm: () => Promise.resolve(true) }).renderCards('inbounds');
  const input = required(document.querySelector<HTMLInputElement>('[data-field="masterLink"]'));
  const help = document.getElementById(required(input.getAttribute('aria-describedby')));
  expect(help?.textContent).toBe('config_master_link_help');
  expect(help?.classList.contains('config-sub')).toBe(true);
});

describe('Discord config', () => {
  it('is optional and requires restart when changed', () => {
    expect(OPTIONAL_KEYS).toContain('discord');
    expect(RESTART_KEYS).toContain('discord');
    expect(validateDraft({}).filter(issue => issue.path === 'discord')).toEqual([]);
    expect(discordIssues({ token: '', whitelist: [] })).toEqual([]);
    expect(discordIssues({ token: discordToken, whitelist: [new ConfigNumber('123456789012345678')] })).toEqual([]);
  });

  it.each([
    null, [], 'token', {}, { token: 1, whitelist: [] }, { token: '' },
    { token: '', whitelist: [0] }, { token: '', whitelist: [-1] },
    { token: '', whitelist: ['123'] }, { token: '', whitelist: [1.5] },
    { token: '', whitelist: [new ConfigNumber('123'), new ConfigNumber('123')] },
    { token: '', whitelist: [], extra: true },
  ])('rejects invalid Discord config: %j', value => {
    expect(discordIssues(value).length).toBeGreaterThan(0);
  });

  it('adds, edits and removes Discord without rounding user IDs', () => {
    document.body.innerHTML = '<div id="config-pane-main"></div>';
    editor.draft = {};
    const dirty = vi.fn();
    createMainEditor(dirty, key => key).renderMain();
    expect(document.querySelector('#cfg-box-discord .config-restart-icon')).not.toBeNull();
    required(document.querySelector<HTMLButtonElement>('[data-optional-add="discord"]')).click();
    expect(editor.draft['discord']).toEqual({ token: '', whitelist: [] });
    const token = requiredInstance(document.getElementById('cfg-discord-token'), HTMLInputElement);
    expect(token.type).toBe('password');
    token.value = discordToken;
    token.dispatchEvent(new Event('input', { bubbles: true }));
    required(document.querySelector<HTMLButtonElement>('[data-add="cfg-rows-discord"]')).click();
    const uid = required(document.querySelector<HTMLInputElement>('#cfg-rows-discord input'));
    uid.value = '123456789012345678';
    uid.dispatchEvent(new Event('input', { bubbles: true }));
    expect(stringifyConfig(required(editor.draft['discord']))).toBe('{"token":"' + discordToken + '","whitelist":[123456789012345678]}');
    expect(discordIssues(required(editor.draft['discord']))).toEqual([]);
    required(document.querySelector<HTMLButtonElement>('[data-optional-remove="discord"]')).click();
    expect(Object.hasOwn(editor.draft, 'discord')).toBe(false);
    expect(dirty).toHaveBeenCalled();
  });
});

describe('strict client config validation', () => {
  const issuesFor = (key: string, value: ConfigValue) => validateDraft({ [key]: value }).filter(issue => issue.path === key);

  it.each(['uri', 'api_uri'])('validates URI path characters and optional surrounding slashes for %s', key => {
    for (const path of ['', '/', 'sub', '/sub', 'sub/', '/sub/', 'admin/api', '/admin/api/', 'sub-name_1.~', '/a%3Fb']) {
      expect(issuesFor(key, path)).toEqual([]);
    }
    for (const path of ['https://example.com', 'https:example.com', '//example.com', '/sub?token=1', '/sub#fragment', 'sub path', '/sub\\path', 'sub\n', '/a/../b', './sub', '/%2e%2e/sub', '/a%2Fb', '/bad%xx', '/%00', '/{sub}', '/<sub>']) {
      expect(issuesFor(key, path).map(issue => issue.message)).toEqual(['config_err_uri_path']);
    }
  });

  it.each([null, [], ['proxy-user', 'proxy-password']])('rejects removed panel authentication: %j', value => {
    const panel = {
      name: 'local', address: '127.0.0.1', port: '2053', uri: '', token: 'x'.repeat(20),
      https: false, whitelist: false, mode: 'blacklist', inbounds_list: [],
    };
    const panelIssues = (config: ConfigValue) => validateDraft({ '3xui': { local: config } }).filter(issue => issue.path.startsWith('3xui.'));
    expect(panelIssues(panel)).toEqual([]);
    expect(panelIssues({ ...panel, nginx_auth: value })).toEqual([
      { path: '3xui.local.nginx_auth', message: 'config_err_unknown' },
    ]);
  });

  it.each(['redis://localhost:6379/0', 'redis://:password@redis:6379/1'])('accepts Redis URL %s', url => {
    expect(issuesFor('redis', { url })).toEqual([]);
  });

  it.each(['localhost:6379', 'http://localhost:6379', 'rediss://localhost:6379', ' redis://localhost:6379'])('rejects Redis URL without redis:// prefix: %s', url => {
    expect(issuesFor('redis', { url }).map(issue => issue.message)).toEqual(['config_err_redis_url']);
  });

  it.each(['android', 'xyz', 'y.xyz', 'com.example.app', 'com.example.app_2'])('accepts package name %s', name => {
    expect(issuesFor('bypass_packages', [name])).toEqual([]);
  });

  it.each(['', ' com.example', 'com.example\n', 'com.example app', '.android', 'android.', 'com..example', 'com.2example', '2android', 'com.example-app', 'com/example', 'com.*'])('rejects invalid package name %j', name => {
    expect(issuesFor('bypass_packages', [name]).map(issue => issue.message)).toEqual(['config_err_package_name']);
  });

  it.each([0, 60, new ConfigNumber('0'), new ConfigNumber('3600')])('accepts non-negative panel alert cooldown %s', value => {
    expect(issuesFor('panel_alert_cooldown', value)).toEqual([]);
  });

  it.each([-1, 1.5, '60', true, new ConfigNumber('-1'), new ConfigNumber('1.0')])('rejects invalid panel alert cooldown %s', value => {
    expect(issuesFor('panel_alert_cooldown', value).map(issue => issue.message)).toEqual(['config_err_positive_integer']);
  });

  it.each(['bot', 'publicbot'])('validates %s Telegram tokens', key => {
    const token = '1234567890:' + 'a_-'.repeat(11) + 'AB';
    const config = { token, ...(key === 'bot' ? { whitelist: [0, new ConfigNumber('42')] } : {}) };
    expect(issuesFor(key, config)).toEqual([]);
    for (const invalid of ['token', '0:' + 'A'.repeat(35), token + '\n', token + 'A', token.replace(':', '.')]) {
      expect(issuesFor(key, { ...config, token: invalid }).length).toBeGreaterThan(0);
    }
  });

  it.each([-1, 1.5, '42', new ConfigNumber('-42'), new ConfigNumber('1.0')])('rejects invalid Telegram admin UID %s', uid => {
    expect(issuesFor('bot', { token: '', whitelist: [uid] }).map(issue => issue.message)).toContain('config_err_positive_integer');
  });

  it('checks snowflake boundaries without rounding', () => {
    for (const raw of ['1', '123456789012345678', '18446744073709551615']) {
      expect(discordIssues({ token: '', whitelist: [new ConfigNumber(raw)] })).toEqual([]);
    }
    for (const uid of [new ConfigNumber('18446744073709551616'), Number.MAX_SAFE_INTEGER + 1]) {
      expect(discordIssues({ token: '', whitelist: [uid] }).length).toBeGreaterThan(0);
    }
  });

  it('checks Discord token segment lengths and characters', () => {
    for (const token of [discordToken, 'A'.repeat(23) + '.' + 'B'.repeat(7) + '.' + 'C'.repeat(27)]) {
      expect(discordIssues({ token, whitelist: [] })).toEqual([]);
    }
    for (const token of ['token', discordToken + '\n', discordToken + 'A', 'Bot ' + discordToken, discordToken.replace('.', '/')]) {
      expect(discordIssues({ token, whitelist: [] }).length).toBeGreaterThan(0);
    }
  });

  it.each(['domain', 'fallback_domain', 'ping_check_url'])('validates HTTP(S) dotted hosts for %s without TLD lookup', key => {
    for (const url of ['https://x.y', 'http://example.invalid:8080', 'https://sub.example.test/']) {
      expect(issuesFor(key, url)).toEqual([]);
    }
    for (const url of ['', 'ftp://x.y', 'https://localhost', 'https://user:pass@x.y', ' https://x.y', 'https://x.y\\path', 'https://-x.y', 'https://x..y', 'https://x.y:65536']) {
      expect(issuesFor(key, url).length).toBeGreaterThan(0);
    }
    for (const url of ['https://x.y/path', 'https://x.y?a=b', 'https://x.y/#fragment']) {
      expect(issuesFor(key, url).length === 0).toBe(key === 'ping_check_url');
    }
  });
});

describe('panel config validation', () => {
  const panel = {
    name: 'local', address: '127.0.0.1', port: '2053', uri: '', token: 'x'.repeat(20),
    https: false, whitelist: false, mode: 'blacklist', inbounds_list: [],
  };
  const panelIssues = (changes: Record<string, ConfigValue>) => validateDraft({ '3xui': { local: { ...panel, ...changes } } })
    .filter(issue => issue.path.startsWith('3xui.'));

  it.each(['127.0.0.1', '0.0.0.0', '255.255.255.255', '::1', '2001:db8::1', '[2001:db8::1]', '::ffff:192.0.2.1', 'example.com', 'sub.example.test', 'localhost', 'example.com.'])('accepts panel address %s', address => {
    expect(panelIssues({ address })).toEqual([]);
  });

  it.each(['', 'https://example.com', 'http://127.0.0.1', 'example.com/path', 'example.com:80', 'user@example.com', 'example.com?x', 'example.com#x', ' example.com', 'example.com\n', 'example.com\\path', '-example.com', 'example..com', '256.1.1.1', '127.1', '01.2.3.4', '2001:db8::xyz', '[::1]:80'])('rejects panel address %j', address => {
    expect(panelIssues({ address }).map(issue => issue.message)).toEqual(['config_err_panel_address']);
  });

  it.each(['', '/', 'secret', '/secret/', '/secret/panel/', '/panel-name_1', '/a%3Fb'])('accepts service path %j', uri => {
    expect(panelIssues({ uri })).toEqual([]);
  });

  it.each(['https://example.com/path', 'https:example.com', '//example.com', '/path?x=1', '/path#fragment', '/a b', '/a\\b', '/a\n', '/a/../b', '/./a', '/%2e%2e/a', '/a%2Fb', '/a%5Cb', '/hello%20world', '/%00', '/bad%xx'])('rejects service path %j', uri => {
    expect(panelIssues({ uri }).map(issue => issue.message)).toEqual(['config_err_panel_path']);
  });

  it.each([0, 65535, '0', '65535', new ConfigNumber('2053')])('accepts port %s', port => {
    expect(panelIssues({ port })).toEqual([]);
  });

  it.each([-1, 65536, 1.5, '', ' ', ' 80', '80\n', '-1', '65536', '1.5', '1e2', true, null, new ConfigNumber('-1')])('rejects port %j', port => {
    expect(panelIssues({ port }).map(issue => issue.message)).toEqual(['config_err_panel_port']);
  });

  it('requires non-negative integer inbound IDs', () => {
    expect(panelIssues({ inbounds_list: [0, 1, new ConfigNumber('42')] })).toEqual([]);
    for (const id of [-1, 1.5, '1', new ConfigNumber('-1')]) {
      expect(panelIssues({ inbounds_list: [id] }).map(issue => issue.message)).toEqual(['config_err_positive_integer']);
    }
  });

  it('limits dedicated whitelist panels independently of mode', () => {
    const panels = { first: { ...panel, whitelist: true }, second: { ...panel, whitelist: true } };
    expect(validateDraft({ '3xui': panels }).filter(issue => issue.path.startsWith('3xui.'))).toEqual([
      { path: '3xui.first.whitelist', message: 'config_err_whitelist_panel_limit' },
      { path: '3xui.second.whitelist', message: 'config_err_whitelist_panel_limit' },
    ]);
    panels.second.whitelist = false;
    expect(validateDraft({ '3xui': panels }).filter(issue => issue.path.startsWith('3xui.'))).toEqual([]);
    expect(panelIssues({ mode: 'whitelist' })).toEqual([]);
  });

  it('shows mode help and updates whitelist errors on all affected panels', () => {
    document.body.innerHTML = '<div id="tab-config"><div id="config-pane-panels"></div></div>';
    editor.draft = { '3xui': { first: { ...panel, whitelist: true }, second: { ...panel } } };
    createCardEditor({ markDirty: vi.fn(), openNew: vi.fn(), t: key => key, language: () => 'en', confirm: () => Promise.resolve(true) }).renderCards('panels');
    expect(document.querySelectorAll('.config-sub')[0]?.textContent).toBe('config_panel_mode_help');
    const toggle = required(document.querySelector<HTMLInputElement>('[data-id="second"] [data-field="whitelist"]'));
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change', { bubbles: true }));
    const errors = [...document.querySelectorAll<HTMLElement>('[id$="-whitelist"].config-error')];
    expect(errors).toHaveLength(2);
    expect(errors.every(error => !error.hidden && error.textContent === 'config_err_whitelist_panel_limit')).toBe(true);
    toggle.checked = false;
    toggle.dispatchEvent(new Event('change', { bubbles: true }));
    expect(errors.every(error => error.hidden)).toBe(true);
  });
});

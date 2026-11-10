import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AdminContext } from '../admin/context';
import { configDispose, configOnTab, initConfig, reloadConfig, saveConfig } from '../admin/config/controller';
import { serializeBody } from '../shared/api';
import { editor } from '../admin/config/state';
import { validateDraft } from '../admin/config/validation';

const config = { uri: '/sub', api_admin_ui_auth: ['admin', 'pass'], provider_id: 'provider', panel_alert_cooldown: 1, salt: 'salt', fingerprints: ['chrome'], nodes: {},
  json_template: { dns: {}, routing: { rules: [], domainStrategy: 'AsIs' }, inbounds: [], outbounds: [], remarks: '' }, bypass_packages: [], domain: 'https://example.com', ping_check_url: 'https://example.com/test',
  '3xui': {}, sub_name: 'Original', profiles: {}, redis: { url: 'redis://localhost' } };
afterEach(() => {
  configDispose();
  editor.loaded = null; editor.draft = null; editor.fetched = false; editor.dirty = false; editor.conflict = false; editor.saving = false; editor.loading = false;
  editor.open.inbounds.clear(); editor.open.panels.clear(); editor.headerRows.clear(); editor.jsonInputs.clear(); editor.nodeRows = null;
});
function setup(can?: AdminContext['can']) {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  required(document.getElementById('tab-config')).classList.add('active');
  const api = vi.fn<AdminContext['api']>();
  const confirm = vi.fn<AdminContext['confirm']>().mockResolvedValue(true);
  const toast = vi.fn();
  initConfig({ doc: document, api, confirm, toast, t: key => key, loadGen: () => () => true, ...(can ? { can } : {}) }, 'en');
  const load = async () => {
    expect(validateDraft(config)).toEqual([]);
    api.mockResolvedValueOnce({ success: true, obj: config, etag: 'v1', status: 200 });
    configOnTab();
    await vi.waitFor(() => { expect(editor.fetched).toBe(true); });
  };
  const editName = (value: string) => {
    const input = requiredInstance(document.getElementById('cfg-sub_name'), HTMLInputElement);
    input.value = value; input.dispatchEvent(new Event('input', { bubbles: true }));
  };
  return { api, confirm, toast, load, editName };
}

describe('config editor controller', () => {
  it('lets read-only scoped operators expand details without permitting edits', async () => {
    const { api } = setup(permission => permission === 'config.read.inbounds');
    requiredInstance(document.querySelector('.config-tab[data-section="main"]'), HTMLButtonElement).disabled = true;
    api.mockResolvedValueOnce({ success: true, status: 200, etag: 'v1', obj: { profiles: { profile: { name: ['Name', 'Имя'], node: 'node', description: ['', ''], shortProfileDescription: ['', ''], flag: '', masterLink: '', whitelist: false } }, nodes: { node: 'host' } } });
    configOnTab(); await vi.waitFor(() => { expect(editor.fetched).toBe(true); });
    const toggle = requiredInstance(document.querySelector('#config-pane-inbounds .config-card-toggle'), HTMLButtonElement);
    expect(toggle.disabled).toBe(false); toggle.click();
    expect(required(document.querySelector('#config-pane-inbounds .panel-card')).classList.contains('open')).toBe(true);
    expect(requiredInstance(document.getElementById('cfg-new-inbounds'), HTMLButtonElement).disabled).toBe(true);
    expect([...document.querySelectorAll<HTMLInputElement>('#config-pane-inbounds input[type="text"]')].every(field => field.readOnly)).toBe(true);
    expect(requiredInstance(document.getElementById('configSave'), HTMLButtonElement).disabled).toBe(true);
    expect(api).toHaveBeenCalledTimes(1);
  });
  it('loads once, marks edits dirty, and saves only changed values with the etag', async () => {
    const { api, load, editName } = setup(); await load();
    configOnTab(); expect(api).toHaveBeenCalledTimes(1);
    editName('Changed');
    expect(editor.dirty).toBe(true);
    expect((requiredInstance(document.getElementById('configSave'), HTMLButtonElement)).disabled).toBe(false);
    api.mockResolvedValueOnce({ success: true, obj: { base: 'v2', restart: ['sub_name'] }, status: 200 });
    await saveConfig();
    const body = required(api.mock.calls[1])[2];
    expect(serializeBody(body)).toBe('{"base":"v1","values":{"sub_name":"Changed"}}');
    expect(editor.etag).toBe('v2');
    expect(editor.dirty).toBe(false);
    expect(required(document.getElementById('configResult')).textContent).toBe('config_saved_state');
    expect(required(document.getElementById('configRestartSaved')).hidden).toBe(false);
  });
  it('retains edits made while a save is in flight', async () => {
    const { api, load, editName } = setup(); await load();
    editName('First');
    let resolve!: (value: { success: boolean; obj: { base: string }; status: number }) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const pending = saveConfig();
    editName('Second');
    resolve({ success: true, obj: { base: 'v2' }, status: 200 });
    await pending;
    expect(editor.loaded?.['sub_name']).toBe('First');
    expect(editor.draft?.['sub_name']).toBe('Second');
    expect(editor.dirty).toBe(true);
  });
  it('preserves local edits on conflict and lets the user discard and reload', async () => {
    const { api, load, editName } = setup(); await load(); editName('Local');
    api.mockResolvedValueOnce({ success: false, msg: 'conflict', status: 409 });
    await saveConfig();
    expect(editor.conflict).toBe(true);
    expect(editor.draft?.['sub_name']).toBe('Local');
    expect((requiredInstance(document.getElementById('configSave'), HTMLButtonElement)).disabled).toBe(true);
    api.mockResolvedValueOnce({ success: true, obj: { ...config, sub_name: 'Remote' }, etag: 'v2', status: 200 });
    required(document.querySelector<HTMLButtonElement>('[data-discard]')).click();
    await vi.waitFor(() => { expect(editor.draft?.['sub_name']).toBe('Remote'); });
    expect(editor.conflict).toBe(false);
  });
  it('does not leak schema errors and confirms before discarding changes', async () => {
    const { api, load, editName, confirm } = setup(); await load(); editName('Local');
    api.mockResolvedValueOnce({ success: false, msg: 'Schema validation error: private token', status: 400 });
    await saveConfig();
    expect(required(document.getElementById('configResult')).textContent).toBe('config_err_schema');
    confirm.mockResolvedValueOnce(false);
    await reloadConfig();
    expect(api).toHaveBeenCalledTimes(2);
    expect(editor.draft?.['sub_name']).toBe('Local');
    expect(await configOnTab.leave()).toBe(true);
    expect(editor.draft?.['sub_name']).toBe('Original');
  });
  it('shows an escaped retryable load failure and ignores responses after disposal', async () => {
    const { api } = setup();
    api.mockResolvedValueOnce({ success: false, msg: '<script>bad</script>', status: 500 });
    configOnTab();
    await vi.waitFor(() => { expect(document.getElementById('configRetry')).not.toBeNull(); });
    expect(required(document.getElementById('configLoad')).querySelector('script')).toBeNull();
    let resolve!: (value: { success: boolean; obj: typeof config; etag: string; status: number }) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    required(document.getElementById('configRetry')).click();
    configDispose();
    resolve({ success: true, obj: config, etag: 'v1', status: 200 });
    await Promise.resolve(); await Promise.resolve();
    expect(editor.fetched).toBe(false);
  });
});

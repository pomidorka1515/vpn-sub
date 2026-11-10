import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AdminContext } from '../admin/context';
import { initHealth } from '../admin/health';
import { createApi } from '../shared/api';
import { createLoadGeneration } from '../shared/stale';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });

function setup(language = 'en') {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  const api = vi.fn<AdminContext['api']>();
  const toast = vi.fn<AdminContext['toast']>();
  const confirm = vi.fn<AdminContext['confirm']>();
  dispose = initHealth({ doc: document, t: key => key, api, toast, confirm, loadGen: createLoadGeneration() }, language);
  const click = (id: string) => { required(document.getElementById(id)).click(); };
  const el = (id: string) => required(document.getElementById(id));
  return { api, toast, confirm, click, el };
}

describe('admin health and recovery', () => {
  it('renders healthy, degraded and failed health responses with safe metric fallbacks', async () => {
    const { api, click, el } = setup();
    api.mockResolvedValueOnce({ success: true, status: 200, obj: { db: true, uptime: 60, threads: '<thread>', memory: { ram: 1.234, swap: Infinity } } })
      .mockResolvedValueOnce({ success: true, status: 200, obj: { degraded: true, memory: null } })
      .mockResolvedValueOnce({ success: false, status: 500, msg: '<failure>' });
    click('btnRunHealth');
    await vi.waitFor(() => { expect(el('healthDot').className).toBe('health-dot ok'); });
    expect(el('healthDetails').textContent).toContain('1.23 mb_unit');
    expect(el('healthDetails').textContent).toContain('<thread>');
    expect(el('healthDetails').querySelector('thread')).toBeNull();
    expect(el('healthDetails').textContent).toContain('na');
    click('btnRunHealth');
    await vi.waitFor(() => { expect(el('healthDot').className).toBe('health-dot warn'); });
    expect(el('healthText').textContent).toBe('health_warn');
    click('btnRunHealth');
    await vi.waitFor(() => { expect(el('healthDot').className).toBe('health-dot err'); });
    expect(el('healthText').textContent).toBe('<failure>');
  });

  it('renders keyed and unkeyed online users, partial data, empty results and errors', async () => {
    const { api, click, el } = setup();
    api.mockResolvedValueOnce({ success: true, status: 200, obj: { users: ['<alice>'], panel_health: { '<panel>': 'down', good: 'ok' } } })
      .mockResolvedValueOnce({ success: true, status: 200, obj: { users: { '<alice>': null, bob: '<external>' } } })
      .mockResolvedValueOnce({ success: true, status: 200, obj: { users: [] } })
      .mockResolvedValueOnce({ success: false, status: 500, msg: '<offline>' });
    click('btnLoadOnline');
    await vi.waitFor(() => { expect(el('onlineUsersWrap').textContent).toContain('<alice>'); });
    expect(el('onlineUsersWrap').querySelector('alice, panel')).toBeNull();
    const keyed = requiredInstance(el('onlineKeyed'), HTMLInputElement);
    keyed.checked = true; keyed.dispatchEvent(new Event('change'));
    await vi.waitFor(() => { expect(el('onlineUsersWrap').textContent).toContain('<external>'); });
    expect(api).toHaveBeenLastCalledWith('GET', '/api/user/onlines?keyed=1');
    expect(el('onlineUsersWrap').textContent).toContain('na');
    click('btnLoadOnline');
    await vi.waitFor(() => { expect(el('onlineUsersWrap').textContent).toContain('no_online'); });
    click('btnLoadOnline');
    await vi.waitFor(() => { expect(el('onlineUsersWrap').textContent).toContain('<offline>'); });
    expect(el('onlineUsersWrap').querySelector('offline')).toBeNull();
  });

  it('renders refresh and teapot success and failure states', async () => {
    const { api, click, el } = setup();
    api.mockResolvedValueOnce({ success: true, status: 200, msg: '<done>' })
      .mockResolvedValueOnce({ success: false, status: 500 })
      .mockResolvedValueOnce({ success: false, status: 418, obj: { teapot: true } })
      .mockResolvedValueOnce({ success: false, status: 418, obj: { teapot: false } })
      .mockResolvedValueOnce({ success: false, status: 403, msg: 'denied', obj: { teapot: true } })
      .mockResolvedValueOnce({ success: false, status: 418, msg: 'invalid', obj: { teapot: 'yes' } })
      .mockResolvedValueOnce(null);
    click('btnDoRefresh');
    await vi.waitFor(() => { expect(el('refreshResult').className).toBe('result-box success'); });
    expect(el('refreshResult').textContent).toContain('<done>');
    expect(el('refreshResult').querySelector('done')).toBeNull();
    click('btnDoRefresh');
    await vi.waitFor(() => { expect(el('refreshResult').className).toBe('result-box error'); });
    expect(el('refreshResult').textContent).toContain('error');
    click('btnTeapot');
    await vi.waitFor(() => { expect(el('teapotStatus').textContent).toContain('teapot_yes'); });
    click('btnTeapot');
    await vi.waitFor(() => { expect(el('teapotStatus').textContent).toBe('teapot_no'); });
    click('btnTeapot');
    await vi.waitFor(() => { expect(el('teapotStatus').textContent).toBe('denied'); });
    click('btnTeapot');
    await vi.waitFor(() => { expect(el('teapotStatus').textContent).toBe('invalid'); });
    click('btnTeapot');
    await vi.waitFor(() => { expect(el('teapotStatus').textContent).toBe('network_err'); });
  });

  it('renders the actual 418 error envelope through the shared transport', async () => {
    document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ success: false, msg: "I'm a teapot", obj: { teapot: false } }), { status: 418 }));
    const api = createApi(() => ({}), key => key, vi.fn(), request);
    dispose = initHealth({ doc: document, t: key => key, api, toast: vi.fn(), confirm: vi.fn(), loadGen: createLoadGeneration() }, 'en');
    required(document.getElementById('btnTeapot')).click();
    await vi.waitFor(() => { expect(required(document.getElementById('teapotStatus')).textContent).toBe('teapot_no'); });
  });

  it('renders recovery issues and resolves only after confirmation', async () => {
    const { api, toast, confirm, click, el } = setup('ru');
    const issue = { daily_snapshot_failure: { ts: 0, failed: 2, eligible: 3 }, rollback_failures: { '<kind>': { '<user>': { ts: 1, reason: '<reason>' }, invalid: null }, invalid: [] }, pending_recovery: { sync: ['<pending>'], invalid: {} } };
    api.mockResolvedValueOnce({ success: true, status: 200, obj: issue })
      .mockResolvedValueOnce({ success: false, status: 500, msg: 'failed' })
      .mockResolvedValueOnce({ success: true, status: 200 })
      .mockResolvedValueOnce({ success: true, status: 200, obj: {} });
    confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true).mockResolvedValueOnce(true);
    click('btnLoadOperations');
    await vi.waitFor(() => { expect(el('operationsStatusWrap').textContent).toContain('<reason>'); });
    expect(el('operationsStatusWrap').textContent).toContain('<pending>');
    expect(el('operationsStatusWrap').querySelector('reason, user, pending')).toBeNull();
    const resolve = () => { required(el('operationsStatusWrap').querySelector<HTMLButtonElement>('[data-rollback-kind]')).click(); };
    resolve();
    await vi.waitFor(() => { expect(confirm).toHaveBeenCalledTimes(1); });
    expect(api).toHaveBeenCalledTimes(1);
    resolve();
    await vi.waitFor(() => { expect(toast).toHaveBeenCalledWith('failed', 'error'); });
    expect(api).toHaveBeenLastCalledWith('POST', '/api/operations/rollback/resolve', { kind: '<kind>', user: '<user>' });
    resolve();
    await vi.waitFor(() => { expect(el('operationsStatusWrap').textContent).toContain('no_recovery_issues'); });
    expect(toast).toHaveBeenCalledWith('resolved', 'success');
  });

  it('ignores stale online responses and pending work after disposal', async () => {
    const { api, click, el } = setup();
    let finish!: (value: Awaited<ReturnType<AdminContext['api']>>) => void;
    api.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce({ success: true, status: 200, obj: { users: ['new'] } });
    click('btnLoadOnline');
    (requiredInstance(el('onlineKeyed'), HTMLInputElement)).dispatchEvent(new Event('change'));
    await vi.waitFor(() => { expect(el('onlineUsersWrap').textContent).toContain('new'); });
    finish({ success: true, status: 200, obj: { users: ['old'] } });
    await Promise.resolve(); await Promise.resolve();
    expect(el('onlineUsersWrap').textContent).not.toContain('old');
    api.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    click('btnRunHealth');
    dispose?.(); dispose = undefined;
    finish({ success: true, status: 200, obj: {} });
    await Promise.resolve(); await Promise.resolve();
    expect(el('healthDot').className).not.toContain('ok');
    click('btnTeapot');
    expect(api).toHaveBeenCalledTimes(3);
  });

  it('disables rollback resolution without root permission', async () => {
    document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
    const api = vi.fn<AdminContext['api']>().mockResolvedValue({ success: true, status: 200, obj: { rollback_failures: { sync: { alice: { reason: 'failed' } } } } });
    const confirm = vi.fn<AdminContext['confirm']>();
    dispose = initHealth({ doc: document, t: key => key, api, toast: vi.fn(), confirm, loadGen: createLoadGeneration(), can: permission => permission === 'health.read.recovery' }, 'en');
    required(document.getElementById('btnLoadOperations')).click();
    await vi.waitFor(() => { expect(document.querySelector('[data-rollback-kind]')).not.toBeNull(); });
    const resolve = required(document.querySelector<HTMLButtonElement>('[data-rollback-kind]'));
    expect(resolve.disabled).toBe(true);
    resolve.disabled = false; resolve.click();
    expect(confirm).not.toHaveBeenCalled();
    expect(api).toHaveBeenCalledTimes(1);
  });
});

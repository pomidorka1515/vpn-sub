import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AdminContext } from '../admin/context';
import { createSystem } from '../admin/system';
import { createLoadGeneration } from '../shared/stale';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
function setup() {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  const api = vi.fn<AdminContext['api']>(); const toast = vi.fn(); const panels = vi.fn(() => '<div>panels</div>');
  const system = createSystem({ doc: document, t: key => key, api, toast, confirm: vi.fn(), loadGen: createLoadGeneration() }, panels, 'en');
  dispose = system.init();
  const click = (id: string) => { required(document.getElementById(id)).click(); };
  return { api, toast, panels, click, system };
}
const status = { cpu: 90, cpu_info: { name: '<cpu>', cores: 4, mhz_max: 2000 }, loadavg: { load_1m: 1.2 }, memory: { ram: { used: 90, total: 100, available: 10 }, swap: { used: 0, total: 0 } },
  network: { recv: 1000, sent: 2000 }, app_memory: { ram: 10, swap: 0 }, uptime: 3600, app_uptime: 60, app_thread_amount: 1, process_count: 3, connections: { tcp: 1, udp: 2 }, ip: { ipv4: ['<ip>'], ipv6: [] }, app_threads: [{ tid: 1, name: '<thread>', state: 'running', cpu: 1.23, stack: 2048, ctx_switches: 4 }] };

describe('admin system monitoring', () => {
  it('renders metrics and thread details while escaping untrusted strings', async () => {
    const { api, click } = setup();
    api.mockResolvedValue({ success: true, obj: status, status: 200 });
    click('btnSystemStatus');
    await vi.waitFor(() => { expect(document.querySelector('#systemStatusResult .thread-card')).not.toBeNull(); });
    const result = required(document.getElementById('systemStatusResult'));
    expect(api).toHaveBeenCalledWith('GET', '/api/state/system');
    expect(result.textContent).toContain('<cpu>');
    expect(result.textContent).toContain('<thread>');
    expect(result.querySelector('cpu, thread, ip')).toBeNull();
    expect(required(result.querySelector('.panel-metric-value.err')).textContent).toBe('90%');
    expect(required(result.querySelector('tbody')).textContent).toContain('1.2');
  });
  it.each([
    { network: { recv: '1', sent: 2 } }, { app_threads: [{ name: 2 }] }, { ip: { ipv4: ['ok', 3] } },
    { memory: { ram: { used: 1, total: 2 } } }, { cpu_info: [] },
  ])('rejects malformed nested status data %j', invalid => {
    const { system } = setup();
    expect(system.renderSystemStatus({ ...status, ...invalid })).toContain('no_data');
  });
  it('validates snapshot cutoff and passes panel data through with unique prefixes', async () => {
    const { api, toast, panels, click } = setup();
    const cutoff = requiredInstance(document.getElementById('snapCutoff'), HTMLInputElement);
    cutoff.value = '0'; click('btnLoadSnapshots');
    expect(toast).toHaveBeenCalledWith('cutoff_invalid', 'error');
    expect(api).not.toHaveBeenCalled();
    cutoff.value = '7';
    api.mockResolvedValue({ success: true, obj: [null, { ts: 0, host: status, panels: { first: { cpu: 1 } } }, { panels: {} }], status: 200 });
    click('btnLoadSnapshots');
    await vi.waitFor(() => { expect(document.querySelectorAll('#snapshotsList > .panel-cards > .panel-card')).toHaveLength(2); });
    expect(api).toHaveBeenCalledWith('POST', '/api/state/snapshots', { cutoff: 7 });
    expect(panels).toHaveBeenNthCalledWith(1, { first: { obj: { cpu: 1 } } }, 'snap-0-');
    expect(panels).toHaveBeenNthCalledWith(2, {}, 'snap-1-');
    expect(required(document.getElementById('snapshotsList')).textContent).toContain('utc');
    expect(required(document.getElementById('snapshotsList')).textContent).toContain('na');
  });
  it('escapes errors and does not update the page after disposal', async () => {
    const { api, click } = setup();
    api.mockResolvedValueOnce({ success: false, msg: '<error>', status: 500 });
    click('btnSystemStatus');
    await vi.waitFor(() => { expect(required(document.getElementById('systemStatusResult')).classList.contains('error')).toBe(true); });
    expect(document.querySelector('#systemStatusResult error')).toBeNull();
    let resolve!: (value: { success: boolean; obj: typeof status; status: number }) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    click('btnSystemStatus');
    dispose?.(); dispose = undefined;
    resolve({ success: true, obj: status, status: 200 });
    await Promise.resolve(); await Promise.resolve();
    expect(document.querySelector('#systemStatusResult .thread-card')).toBeNull();
  });
});

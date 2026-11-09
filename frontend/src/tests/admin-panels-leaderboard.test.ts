import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AdminContext } from '../admin/context';
import { initLeaderboard } from '../admin/leaderboard';
import { createPanels } from '../admin/panels';
import { createLoadGeneration } from '../shared/stale';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
function setup() {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  const api = vi.fn<AdminContext['api']>();
  const context: AdminContext = { doc: document, t: key => key, api, toast: vi.fn(), confirm: vi.fn(), loadGen: createLoadGeneration() };
  const click = (id: string) => document.getElementById(id)!.click();
  return { api, context, click };
}
const success = (obj: unknown) => ({ success: true, obj, status: 200 });
const metrics = { cpu: 85, uptime: 3600, mem: { current: 90, total: 100 }, swap: { current: 0, total: 0 }, disk: { current: 80, total: 100 }, appStats: { mem: 50, threads: 3 }, netIO: { down: 1, up: 2 }, netTraffic: { recv: 3, sent: 4 }, xray: { state: 'running', version: '1', errorMsg: '<error>' }, publicIP: { ipv4: '1.2.3.4' }, loads: [1.25] };

describe('admin panel status', () => {
  it('encodes filters, renders metrics safely and toggles cards', async () => {
    const { api, context, click } = setup();
    dispose = createPanels(context).init();
    (document.getElementById('panelName') as HTMLInputElement).value = '  a&b  ';
    api.mockResolvedValue(success({ '<panel>': { obj: metrics } }));
    click('btnPanelStatus');
    await vi.waitFor(() => expect(document.querySelector('.panel-card')).not.toBeNull());
    expect(api).toHaveBeenCalledExactlyOnceWith('GET', '/api/panel/status?name=a%26b');
    const card = document.querySelector<HTMLElement>('.panel-card')!;
    expect(card.textContent).toContain('<panel>');
    expect(card.querySelector('panel')).toBeNull();
    expect(card.textContent).toContain('<error>');
    expect(card.querySelector('.panel-metric-value.err')!.textContent).toBe('85%');
    (card.querySelector('.panel-card-header') as HTMLElement).click();
    expect(card.classList.contains('open')).toBe(true);
  });
  it('handles empty, malformed and failed responses without rendering injected HTML', async () => {
    const { api, context, click } = setup();
    const panels = createPanels(context); dispose = panels.init();
    expect(panels.renderPanelCards({})).toContain('no_panels');
    const html = panels.renderPanelCards({ broken: { obj: { ...metrics, loads: ['bad'] } } });
    expect(html).toContain('unknown');
    expect(html).not.toContain('85%');
    api.mockResolvedValue({ success: false, msg: '<img src=x>', status: 500 });
    click('btnPanelStatus');
    await vi.waitFor(() => expect(document.getElementById('panelStatusResult')!.classList.contains('error')).toBe(true));
    expect(document.getElementById('panelStatusResult')!.querySelector('img')).toBeNull();
    expect(document.getElementById('panelStatusResult')!.textContent).toContain('<img src=x>');
  });
});

describe('leaderboard', () => {
  it('clamps negative cutoff, filters malformed entries and escapes user names', async () => {
    const { api, context, click } = setup();
    dispose = initLeaderboard(context);
    (document.getElementById('lbCutoff') as HTMLInputElement).value = '-9';
    api.mockResolvedValue(success([{ place: 1, username: '<img>', amount: 1000 }, null, { place: 2, username: 'bob', amount: 'bad' }]));
    click('btnFetchLb');
    await vi.waitFor(() => expect(document.querySelectorAll('#lbTableWrap tbody tr')).toHaveLength(2));
    expect(api).toHaveBeenCalledWith('POST', '/api/leaderboard', expect.objectContaining({ cutoff: 0 }));
    expect(document.getElementById('lbTableWrap')!.querySelector('img')).toBeNull();
    expect(document.getElementById('lbTableWrap')!.textContent).toContain('<img>');
    expect(document.getElementById('lbTableWrap')!.textContent).toContain('na');
  });
  it('displays empty and escaped failure messages', async () => {
    const { api, context, click } = setup(); dispose = initLeaderboard(context);
    api.mockResolvedValueOnce(success([])).mockResolvedValueOnce({ success: false, msg: '<error>', status: 500 });
    click('btnFetchLb');
    await vi.waitFor(() => expect(document.getElementById('lbTableWrap')!.textContent).toBe('no_entries'));
    click('btnFetchLb');
    await vi.waitFor(() => expect(document.getElementById('lbTableWrap')!.textContent).toBe('<error>'));
    expect(document.getElementById('lbTableWrap')!.querySelector('error')).toBeNull();
  });
});

import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AdminContext } from '../admin/context';
import { createCharts } from '../admin/charts/controller';
import { createLoadGeneration } from '../shared/stale';

const renderer = vi.hoisted(() => ({ destroyCharts: vi.fn(), renderGroup: vi.fn(), updateGroup: vi.fn(() => true) }));
vi.mock('../admin/charts/render', () => ({ createChartRenderer: () => renderer }));

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
function setup() {
  document.body.innerHTML = readFileSync('src/templates/admin.html', 'utf8');
  document.getElementById('tab-charts')!.classList.add('active');
  vi.stubGlobal('Chart', class {});
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
  const api = vi.fn<AdminContext['api']>();
  const toast = vi.fn();
  const context: AdminContext = { doc: document, t: key => key, api, toast, confirm: vi.fn(), loadGen: createLoadGeneration() };
  const request = vi.fn<typeof fetch>();
  const on401 = vi.fn();
  const config = { baseUrl: () => '/admin', headers: () => ({ Authorization: 'token' }), guard: () => false, on401 };
  const charts = createCharts(context, config, '/chart.js', 20, 'en', request);
  dispose = charts.initCharts();
  const click = (selector: string) => document.querySelector<HTMLElement>(selector)!.click();
  return { api, toast, request, on401, charts, click };
}
const success = (obj: unknown) => ({ success: true, obj, status: 200 });

describe('admin chart controller', () => {
  it('loads snapshots, drops malformed rows, reverses them and creates escaped panel selectors', async () => {
    const { api, charts, click } = setup();
    api.mockResolvedValue(success([{ ts: 2, host: { cpu: 20 }, panels: { '<panel>': {} } }, { ts: 'bad' }, { ts: 1, host: { cpu: 10 }, panels: {} }, { ts: 3, panels: [] }]));
    charts.chartsOnTab('charts');
    await vi.waitFor(() => expect(renderer.renderGroup).toHaveBeenCalled());
    expect(api).toHaveBeenCalledWith('POST', '/api/state/snapshots', { cutoff: 7 });
    expect(renderer.renderGroup.mock.calls.at(-1)![2]).toEqual([{ ts: 1, host: { cpu: 10 }, panels: {} }, { ts: 2, host: { cpu: 20 }, panels: { '<panel>': {} } }]);
    const panel = document.querySelector<HTMLElement>('[data-scope="<panel>"]')!;
    expect(panel.textContent).toBe('<panel>');
    expect(panel.querySelector('panel')).toBeNull();
    click('[data-scope="<panel>"]');
    expect(panel.getAttribute('aria-pressed')).toBe('true');
    expect(renderer.renderGroup.mock.calls.at(-1)![3]).toBe('<panel>');
  });
  it('discards out-of-order snapshot responses after the cutoff changes', async () => {
    const { api, charts } = setup();
    let old!: (value: ReturnType<typeof success>) => void;
    api.mockImplementationOnce(() => new Promise(done => { old = done; })).mockResolvedValueOnce(success([{ ts: 2, host: {}, panels: {} }]));
    charts.chartsOnTab('charts');
    const cutoff = document.getElementById('chartsCutoff') as HTMLInputElement;
    cutoff.value = '3'; cutoff.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(renderer.renderGroup).toHaveBeenCalled());
    old(success([{ ts: 1, host: {}, panels: {} }]));
    await Promise.resolve(); await Promise.resolve();
    expect(renderer.renderGroup).toHaveBeenCalledTimes(1);
    expect(renderer.renderGroup.mock.calls[0]![2]).toEqual([{ ts: 2, host: {}, panels: {} }]);
  });
  it('reports failed snapshot loads and validates cutoff input', async () => {
    const { api, charts, toast } = setup();
    api.mockResolvedValue({ success: false, msg: 'offline', status: 500 });
    charts.chartsOnTab('charts');
    await vi.waitFor(() => expect(toast).toHaveBeenCalledWith('offline', 'error'));
    expect(document.getElementById('chartsStatus')!.textContent).toBe('network_err');
    const cutoff = document.getElementById('chartsCutoff') as HTMLInputElement;
    cutoff.value = '0'; cutoff.dispatchEvent(new Event('change'));
    expect(toast).toHaveBeenCalledWith('cutoff_invalid', 'error');
    expect(api).toHaveBeenCalledTimes(1);
  });
  it('starts an authenticated stream in live mode and stops on unauthorized', async () => {
    const { api, charts, click, request, on401 } = setup();
    api.mockResolvedValue(success([]));
    request.mockResolvedValue(new Response('', { status: 401 }));
    charts.chartsOnTab('charts');
    await vi.waitFor(() => expect(renderer.renderGroup).toHaveBeenCalled());
    click('[data-mode="live"]');
    await vi.waitFor(() => expect(on401).toHaveBeenCalledOnce());
    expect(request).toHaveBeenCalledWith('/admin/api/state/polling', expect.objectContaining({ credentials: 'same-origin', headers: { Authorization: 'token', Accept: 'text/event-stream' }, signal: expect.any(AbortSignal) }));
    expect(document.getElementById('chartsCutoffField')!.hidden).toBe(true);
    expect(document.getElementById('chartsDelayField')!.hidden).toBe(false);
  });
  it('aborts a live stream on tab exit and does not report an intentional abort', async () => {
    const { api, charts, click, request, toast } = setup();
    api.mockResolvedValue(success([]));
    let signal!: AbortSignal;
    request.mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      signal = init!.signal!;
      signal.addEventListener('abort', () => reject(new Error('aborted')));
    }));
    charts.chartsOnTab('charts');
    await vi.waitFor(() => expect(renderer.renderGroup).toHaveBeenCalled());
    click('[data-mode="live"]');
    await vi.waitFor(() => expect(request).toHaveBeenCalledOnce());
    charts.chartsOnTab('users');
    expect(signal.aborted).toBe(true);
    await Promise.resolve(); await Promise.resolve();
    expect(toast).not.toHaveBeenCalled();
  });
  it('parses split event-stream frames, drops only the baseline CPU sample and ignores malformed frames', async () => {
    const { api, charts, click, request, toast } = setup();
    api.mockResolvedValue(success([]));
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } });
    request.mockResolvedValue(new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }));
    vi.spyOn(Date, 'now').mockReturnValue(10000);
    charts.chartsOnTab('charts');
    await vi.waitFor(() => expect(renderer.renderGroup).toHaveBeenCalled());
    click('[data-mode="live"]');
    await vi.waitFor(() => expect(request).toHaveBeenCalledOnce());
    const send = (text: string) => stream.enqueue(new TextEncoder().encode(text));
    send('data: {"success":true,"obj":{"host":{"cpu":');
    send('0},"panels":{}}}\n\n');
    await vi.waitFor(() => expect(renderer.renderGroup.mock.calls.at(-1)![2]).toEqual([{ ts: 10, host: { cpu: null }, panels: {} }]));
    vi.mocked(Date.now).mockReturnValue(13000);
    send('data: {"success":true,"obj":{"host":{"cpu":0},"panels":{}}}\n\n');
    await vi.waitFor(() => expect(renderer.renderGroup.mock.calls.at(-1)![2]).toHaveLength(2));
    expect(renderer.renderGroup.mock.calls.at(-1)![2][1].host.cpu).toBe(0);
    send('data: {"success":false}\n\n');
    await vi.waitFor(() => expect(toast).toHaveBeenCalledWith('network_err', 'error'));
    charts.chartsOnTab('users');
    stream.close();
  });
});

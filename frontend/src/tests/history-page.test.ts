import { required, requiredInstance } from './required';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initHistory } from '../history/page';

interface ChartConfig {
  data: { labels: string[]; datasets: { label: string; data: number[] }[] };
  options: object;
}
class FakeChart {
  static instances: FakeChart[] = [];
  data: ChartConfig['data'];
  options: ChartConfig['options'];
  destroy = vi.fn();
  update = vi.fn();
  constructor(_ctx: unknown, config: ChartConfig) {
    this.data = config.data;
    this.options = config.options;
    FakeChart.instances.push(this);
  }
}
let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  dispose = undefined;
  FakeChart.instances = [];
});

function setup(request: typeof fetch, chart = true) {
  const html = readFileSync('src/templates/history.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script');
  bootstrap.type = 'application/json';
  bootstrap.id = 'frontend-bootstrap';
  bootstrap.textContent = JSON.stringify({
    prefix: '/sub',
    language: 'en',
    translations: {},
    fallback: {},
    page: { chartUrl: '/chart.js', debounceMs: 20 },
  });
  document.head.append(bootstrap);
  vi.stubGlobal('fetch', request);
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false })),
  );
  if (chart) vi.stubGlobal('Chart', FakeChart);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  vi.spyOn(Date, 'now').mockReturnValue(10 * 86400 * 1000);
  dispose = initHistory(document, window);
}
const sample = (day: number, up: number, down: number, wl_up = 0, wl_down = 0) => ({
  ts: day * 86400,
  up,
  down,
  wl_up,
  wl_down,
});

describe('history page', () => {
  it('plots sorted regular and whitelist traffic separately and totals the selected samples', async () => {
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ success: true, obj: [sample(10, 1000, 2000, 3000, 4000), sample(9, 5, 10)] }));
    setup(request);
    await vi.waitFor(() => {
      expect(FakeChart.instances).toHaveLength(2);
    });
    expect(request).toHaveBeenCalledWith('/sub/webapi/history?days=30', expect.anything());
    expect(required(FakeChart.instances[0]).data.datasets.map((set) => set.data)).toEqual([
      [10, 2000],
      [5, 1000],
    ]);
    expect(required(FakeChart.instances[1]).data.datasets.map((set) => set.data)).toEqual([
      [0, 4000],
      [0, 3000],
    ]);
    expect(required(document.getElementById('regSummary')).textContent).toContain('3 kb_unit');
    expect(required(document.getElementById('wlSummary')).textContent).toContain('7 kb_unit');
    dispose?.();
    dispose = undefined;
    expect(FakeChart.instances.every((chart) => chart.destroy.mock.calls.length === 1)).toBe(true);
  });
  it('shows no data for zero traffic and retains the last successful data on a failed refresh', async () => {
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ success: true, obj: [sample(10, 0, 0)] }))
      .mockResolvedValueOnce(Response.json({ success: false, msg: null }, { status: 500 }));
    setup(request);
    await vi.waitFor(() => {
      expect(required(document.getElementById('regEmpty')).textContent).toBe('no_data');
    });
    expect(FakeChart.instances).toHaveLength(0);
    required(document.getElementById('btnRefresh')).click();
    await vi.waitFor(() => {
      expect(required(document.querySelector('.toast')).textContent).toBe('network_err');
    });
    expect(required(document.getElementById('regEmpty')).textContent).toBe('no_data');
    expect(required(document.getElementById('regSummary')).textContent).toContain('0 byte_unit');
  });
  it('ignores an outdated request after the range changes', async () => {
    let resolve!: (response: Response) => void;
    const request = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      )
      .mockResolvedValueOnce(Response.json({ success: true, obj: [sample(10, 0, 2000)] }));
    setup(request);
    const range = requiredInstance(document.getElementById('daysRange'), HTMLInputElement);
    range.value = '2';
    range.dispatchEvent(new Event('input'));
    expect(required(document.getElementById('daysValue')).textContent).toBe('2');
    resolve(Response.json({ success: true, obj: [sample(10, 0, 9000)] }));
    await vi.waitFor(() => {
      expect(FakeChart.instances).toHaveLength(1);
    });
    expect(required(required(FakeChart.instances[0]).data.datasets[0]).data).toEqual([2000]);
    expect(request).toHaveBeenCalledTimes(2);
  });
  it('reports chart script failures instead of trying to render data', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ success: true, obj: [sample(10, 1, 2)] }));
    setup(request, false);
    const script = required(document.querySelector<HTMLScriptElement>('script[src="/chart.js"]'));
    expect(script).not.toBeNull();
    script.dispatchEvent(new Event('error'));
    await vi.waitFor(() => {
      expect(required(document.getElementById('regEmpty')).textContent).toBe('chart_load_err');
    });
    expect(required(document.querySelector('.toast')).textContent).toBe('chart_load_err');
  });
  it('does not render an in-flight response after disposal', async () => {
    let resolve!: (response: Response) => void;
    const request = vi.fn<typeof fetch>().mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    setup(request);
    dispose?.();
    dispose = undefined;
    resolve(Response.json({ success: true, obj: [sample(10, 1, 2)] }));
    await Promise.resolve();
    await Promise.resolve();
    expect(FakeChart.instances).toHaveLength(0);
  });
});

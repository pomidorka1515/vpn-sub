import { required } from './required';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createChartRenderer } from '../admin/charts/render';
import { chartColors, seriesColor, seriesFill } from '../admin/charts/theme';
import type { ChartMap, Scales } from '../admin/charts/render';
import type { ChartSpec } from '../admin/charts/spec';

interface Point {
  x: number;
  y: number | null;
}
interface Dataset {
  data: Point[];
  unit: string;
  yAxisID: string;
}
interface Options {
  data: { datasets: Dataset[] };
  options: { scales: { x: { min?: number; max?: number }; y: { min?: number; max?: number } } };
}
class FakeChart {
  static instances: FakeChart[] = [];
  data: Options['data'];
  options: Options['options'];
  update = vi.fn();
  destroy = vi.fn();
  resize = vi.fn();
  constructor(
    public canvas: HTMLCanvasElement,
    options: Options,
  ) {
    this.data = options.data;
    this.options = options.options;
    FakeChart.instances.push(this);
  }
}
const cpu: ChartSpec = {
  id: 'cpu',
  titleKey: 'cpu',
  unit: 'percent',
  threshold: 'cpu',
  series: [{ key: 'cpu', labelKey: 'cpu', path: ['cpu'] }],
};
const memory: ChartSpec = {
  id: 'memory',
  titleKey: 'memory',
  unit: 'bytes',
  threshold: 'memory',
  series: [
    { key: 'used', labelKey: 'used', path: ['mem', 'used'] },
    {
      key: 'percent',
      labelKey: 'percent',
      path: ['mem', 'used'],
      totalPath: ['mem', 'total'],
      scale: 'percent',
      axis: 'y1',
      unit: 'percent',
    },
  ],
};
afterEach(() => {
  FakeChart.instances = [];
});
function setup() {
  document.body.innerHTML = '<div id="chartsMount"></div>';
  vi.stubGlobal('Chart', FakeChart);
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false })),
  );
  vi.stubGlobal('CSS', { escape: (value: string) => value });
  const renderer = createChartRenderer((key) => key, 'en');
  const map: ChartMap = new Map();
  const scales: Scales = new Map();
  const mount = required(document.getElementById('chartsMount'));
  return { ...renderer, map, scales, mount };
}

describe('chart rendering', () => {
  it('uses the soft palette and reads theme colors for each render', () => {
    const root = document.documentElement;
    try {
      expect(seriesColor(0)).toBe('#a8c9bc');
      expect(seriesFill(seriesColor(1))).toBe('rgba(217,170,128,0.16)');
      root.style.setProperty('--green', '#285d49');
      root.style.setProperty('--red', '#982e2e');
      expect(chartColors()).toMatchObject({ ok: '#285d49', err: '#982e2e' });
      expect(seriesColor(0, chartColors())).toBe('#285d49');
    } finally {
      root.style.removeProperty('--green');
      root.style.removeProperty('--red');
    }
  });
  it('renders missing values as gaps, uses the latest valid readout and marks thresholds', () => {
    const { renderGroup, map, scales, mount, destroyCharts } = setup();
    renderGroup(
      mount,
      [cpu, memory],
      [
        { ts: 1, host: { cpu: 85, mem: { used: 900, total: 1000 } } },
        { ts: 2, host: { cpu: null, mem: { used: 0, total: 0 } } },
      ],
      'host',
      'snapshot',
      map,
      scales,
    );
    expect(FakeChart.instances).toHaveLength(2);
    expect(required(required(FakeChart.instances[0]).data.datasets[0]).data).toEqual([
      { x: 1, y: 85 },
      { x: 2, y: null },
    ]);
    const readout = required(mount.querySelector('[data-chart-id="cpu"] .charts-card-value'));
    expect(readout.textContent).toBe('85%');
    expect(readout.classList.contains('err')).toBe(true);
    expect(required(required(FakeChart.instances[1]).data.datasets[1]).data).toEqual([
      { x: 1, y: 90 },
      { x: 2, y: null },
    ]);
    expect(required(required(FakeChart.instances[1]).data.datasets[1]).yAxisID).toBe('y1');
    destroyCharts(map);
    expect(map.size).toBe(0);
    expect(FakeChart.instances.every((chart) => chart.destroy.mock.calls.length === 1)).toBe(true);
  });
  it('updates live points in place, replaces duplicate timestamps and shifts old points', () => {
    const { renderGroup, updateGroup, map, scales, mount } = setup();
    const live = { now: 2, windowS: 90, duration: 500 };
    renderGroup(
      mount,
      [cpu],
      [
        { ts: 1, host: { cpu: 10 } },
        { ts: 2, host: { cpu: 20 } },
      ],
      'host',
      'live',
      map,
      scales,
      live,
    );
    const chart = required(FakeChart.instances[0]);
    expect(updateGroup([cpu], [{ ts: 2, host: { cpu: 25 } }], 'host', 'live', map, scales, live)).toBe(true);
    expect(required(chart.data.datasets[0]).data).toEqual([{ x: 2, y: 25 }]);
    expect(chart.options.scales.x).toMatchObject({ min: -88, max: 2 });
    expect(chart.update).toHaveBeenCalledWith('none');
    expect(required(mount.querySelector('.charts-card-value')).textContent).toBe('25%');
    expect(FakeChart.instances).toHaveLength(1);
  });
  it('switches between empty and plotted states and rejects an incompatible chart map', () => {
    const { renderGroup, updateGroup, map, scales, mount } = setup();
    renderGroup(mount, [cpu], [{ ts: 1, host: {} }], 'host', 'snapshot', map, scales);
    const chart = required(FakeChart.instances[0]);
    expect(chart.canvas.hidden).toBe(true);
    expect(required(mount.querySelector<HTMLElement>('.charts-empty')).hidden).toBe(false);
    updateGroup([cpu], [{ ts: 2, host: { cpu: 0 } }], 'host', 'snapshot', map, scales);
    expect(chart.canvas.hidden).toBe(false);
    expect(chart.resize).toHaveBeenCalledOnce();
    expect(required(mount.querySelector<HTMLElement>('.charts-empty')).hidden).toBe(true);
    expect(updateGroup([memory], [], 'host', 'snapshot', map, scales)).toBe(false);
  });
  it('reads the selected panel only and does not inherit prototype properties', () => {
    const { renderGroup, map, scales, mount } = setup();
    renderGroup(
      mount,
      [cpu],
      [
        { ts: 1, host: { cpu: 9 }, panels: { p: { cpu: 50 } } },
        { ts: 2, panels: {} },
      ],
      'p',
      'snapshot',
      map,
      scales,
    );
    expect(required(required(FakeChart.instances[0]).data.datasets[0]).data).toEqual([
      { x: 1, y: 50 },
      { x: 2, y: null },
    ]);
    renderGroup(mount, [cpu], [{ ts: 1, panels: {} }], 'toString', 'snapshot', map, scales);
    expect(required(required(FakeChart.instances[1]).data.datasets[0]).data).toEqual([{ x: 1, y: null }]);
  });
});

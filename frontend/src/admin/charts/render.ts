import { sharedMessage } from '../../shared/messages';
import { fmtBytes as formatBytes, fmtUptime as formatUptime } from '../../shared/formatting';
import type { Translate } from '../../shared/formatting';
import { readSeries } from './series';
import { axisMax, windowedRange } from './scale';
import { chartColors, seriesColor, seriesFill, baseOptions, reducedMotion } from './theme';
import type { ChartColors } from './theme';
import type { ChartSpec, ChartSeriesSpec } from './spec';

export interface Row { ts: number; host?: unknown; panels?: Record<string, unknown> }
export interface Live { now: number; windowS: number; duration: number }
interface AxisScale { div: number; unit: string }
interface Point { x: number; y: number | null }
interface Dataset {
  key: string; label: string; labelKey?: string; unit: string; yMax: number; yMin: number;
  scale: AxisScale | null; data: Point[]; yAxisID: string; pointRadius: number;
  borderColor?: string; backgroundColor?: string; fill?: boolean | string; tension?: number;
  spanGaps?: boolean; pointHoverRadius?: number; borderWidth?: number;
}
interface Tooltip { datasetIndex: number; parsed: { y: number }; dataset: { label: string } }
interface Axis { min?: number; max?: number; grace?: number; ticks?: { callback: (value: number) => string } }
interface ChartInstance {
  data: { datasets: Dataset[] };
  options: { scales?: { x?: Axis; y?: Axis }; plugins?: { tooltip?: { callbacks?: { label: (item: Tooltip) => string } } }; animation?: { x?: { duration: number }; y?: { duration: number } } };
  canvas: HTMLCanvasElement;
  destroy(): void; update(mode?: string): void; resize(): void;
}
type ChartConstructor = new (canvas: HTMLCanvasElement, options: object) => ChartInstance;
export type ChartMap = Map<string, ChartInstance>;
export type Scales = Map<string, number>;

export function createChartRenderer(t: Translate, language: string) {
const fmtBytes = (value: number): string => formatBytes(value, t);
const fmtUptime = (value: number): string => formatUptime(value, t);

const BYTE_UNITS = ['byte_unit', 'kb_unit', 'mb_unit', 'gb_unit', 'tb_unit'].map(t);

function axisBytes(max: number): AxisScale {
  if (!Number.isFinite(max) || max <= 0) return { div: 1, unit: t('byte_unit') };
  const i = Math.min(Math.floor(Math.log(max) / Math.log(1024)), BYTE_UNITS.length - 1);
  return { div: 1024 ** i, unit: BYTE_UNITS[i] ?? t('byte_unit') };
}

function formatValue(value: number | null | undefined, unit: string, scale?: AxisScale | null): string {
  if (value == null || Number.isNaN(value)) return t('na');
  if (unit === 'bytes' || unit === 'bytes_per_s') {
    const suffix = unit === 'bytes_per_s' ? t('per_second') : '';
    if (scale) {
      const scaled = value / scale.div;
      const digits = scale.div === 1 ? 0 : 1;
      return scaled.toFixed(digits) + ' ' + scale.unit + suffix;
    }
    return fmtBytes(value) + suffix;
  }
  if (unit === 'seconds') return fmtUptime(value);
  if (unit === 'percent') return (Math.round(value * 100) / 100) + '%';
  if (unit === 'count') return String(Math.round(value));
  return (Math.round(value * 100) / 100).toString();
}

function thresholdClass(kind: string | undefined, value: number | null): string {
  if (value == null || kind == null) return '';
  if (kind === 'cpu') return value > 80 ? 'err' : value > 50 ? 'warn' : 'ok';
  if (kind === 'memory') return value > 85 ? 'err' : value > 65 ? 'warn' : 'ok';
  if (kind === 'disk') return value > 90 ? 'err' : value > 75 ? 'warn' : 'ok';
  return '';
}

function lastValue(points: Array<number | null>): number | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const point = points[i];
    if (point != null) return point;
  }
  return null;
}

function sourceOf(row: Row | undefined, scope: string): unknown {
  if (!row) return null;
  if (scope === 'host') return row.host ?? null;
  const panels = row.panels;
  if (!panels || typeof panels !== 'object') return null;
  return Object.prototype.hasOwnProperty.call(panels, scope) ? panels[scope] : null;
}

function seriesPoints(rows: Row[], scope: string, spec: ChartSeriesSpec): Array<number | null> {
  return rows.map(row => readSeries(sourceOf(row, scope), spec));
}

function pointRadiusFor(count: number): number {
  return count > (window.matchMedia('(max-width: 600px)').matches ? 16 : 24) ? 0 : 1.5;
}

function windowedYs(rows: Row[], values: Array<number | null>, now?: number, windowS?: number): number[] {
  const inWindow = now !== undefined && windowS !== undefined && Number.isFinite(now) && Number.isFinite(windowS);
  const ys: number[] = [];
  for (let i = 0; i < values.length; i++) {
    const y = values[i];
    if (y == null || !Number.isFinite(y)) continue;
    if (inWindow) {
      const x = rows[i]?.ts;
      if (x === undefined || !Number.isFinite(x) || x < now - windowS || x > now) continue;
    }
    ys.push(y);
  }
  return ys;
}

function boundsOf(ys: number[]): { min: number; max: number } {
  if (!ys.length) return { min: 0, max: 0 };
  return { min: Math.min(...ys), max: Math.max(...ys) };
}

function seriesBounds(spec: ChartSpec, rows: Row[], raw: Array<Array<number | null>>, now?: number, windowS?: number): { min: number; max: number } {
  const ys: number[] = [];
  spec.series.forEach((series, index) => {
    if (series.axis === 'y1') return;
    ys.push(...windowedYs(rows, raw[index] ?? [], now, windowS));
  });
  return boundsOf(ys);
}

function labelFor(ts: number, mode: string): string {
  const d = new Date(ts * 1000);
  if (mode === 'snapshot') {
    return d.toLocaleDateString(language === 'ru' ? 'ru-RU' : 'en-US', {
      timeZone: 'UTC', month: 'short', day: 'numeric',
    });
  }
  return d.toLocaleTimeString(language === 'ru' ? 'ru-RU' : 'en-US', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function chartHasData(datasets: Dataset[]): boolean {
  return datasets.some(ds => ds.data.some(point => point != null && point.y != null));
}

function applyReadout(el: HTMLElement | null, chart: ChartSpec, datasets: Dataset[]): void {
  if (!el) return;
  const readout = readoutText(chart, datasets);
  el.textContent = readout.text;
  el.className = 'charts-card-value' + (readout.cls ? ' ' + readout.cls : '');
}

function readoutText(chart: ChartSpec, datasets: Dataset[]): { text: string; cls: string } {
  const primary = chart.series.find(s => s.axis !== 'y1') || chart.series[0];
  const ds = primary ? datasets.find(d => d.key === primary.key) || datasets[0] : undefined;
  if (!ds) return { text: t('na'), cls: '' };
  const value = lastValue(ds.data.map(point => point.y));
  if (value == null) return { text: t('na'), cls: '' };
  let cls = '';
  if (chart.threshold) {
    const pct = chart.series.find(s => s.scale === 'percent');
    const pctDs = pct ? datasets.find(d => d.key === pct.key) : null;
    const pctValue = chart.unit === 'percent' ? value : (pctDs ? lastValue(pctDs.data.map(point => point.y)) : null);
    cls = thresholdClass(chart.threshold, pctValue);
  }
  return { text: formatValue(value, ds.unit || chart.unit), cls };
}

function buildDatasets(chart: ChartSpec, rows: Row[], scope: string, mode: string, colors: ChartColors, heldMax: number, live?: Live): Dataset[] {
  const raw = chart.series.map(spec => seriesPoints(rows, scope, spec));
  const liveOn = mode === 'live' && live !== undefined && Number.isFinite(live.now);
  const bounds = seriesBounds(chart, rows, raw, liveOn ? live.now : undefined, liveOn ? live.windowS : undefined);
  const range = liveOn
    ? windowedRange(chart.unit, bounds.min, bounds.max, heldMax, true)
    : { min: 0, max: axisMax(chart.unit, bounds.max, heldMax) };
  const bytes = chart.unit === 'bytes' || chart.unit === 'bytes_per_s';
  const scale = bytes ? axisBytes(range.max) : null;
  const radius = pointRadiusFor(rows.length);
  return chart.series.map((spec, index) => {
    const color = seriesColor(index, colors);
    const values = raw[index] ?? [];
    return {
      key: spec.key,
      label: t(spec.labelKey),
      labelKey: spec.labelKey,
      unit: spec.unit || chart.unit,
      yMax: range.max,
      yMin: range.min,
      scale: spec.axis === 'y1' ? null : scale,
      data: rows.map((row, i) => ({ x: row.ts, y: values[i] ?? null })),
      borderColor: color,
      backgroundColor: seriesFill(color),
      yAxisID: spec.axis === 'y1' ? 'y1' : 'y',
      fill: spec.axis === 'y1' ? false : 'origin',
      tension: 0.25,
      spanGaps: false,
      pointRadius: radius,
      pointHoverRadius: 2.25,
      borderWidth: 1.5,
    };
  });
}

function appendPoint(target: Dataset, point: Point): void {
  const data = target.data;
  const last = data[data.length - 1];
  if (last && last.x === point.x) {
    last.y = point.y;
    return;
  }
  data.push({ x: point.x, y: point.y });
}

function shiftTo(target: Dataset, x: number): void {
  const data = target.data;
  while (data[0] && data[0].x < x) data.shift();
}

function makeChart(canvas: HTMLCanvasElement, chart: ChartSpec, datasets: Dataset[], mode: string, live?: Live): ChartInstance {
  const colors = chartColors();
  const scale = datasets.find(ds => ds.yAxisID !== 'y1' && ds.scale)?.scale || null;
  const formatTick = (value: number): string => {
    return formatValue(value, chart.unit, scale);
  };
  const options = baseOptions(colors, {
    live: mode === 'live',
    yMax: datasets.find(ds => ds.yAxisID !== 'y1')?.yMax ?? 0,
    yMin: datasets.find(ds => ds.yAxisID !== 'y1')?.yMin ?? 0,
    dual: chart.series.some(s => s.axis === 'y1'),
    unit: chart.unit,
    formatTick,
    formatTooltip: (value, tipUnit) => formatValue(value, tipUnit),
    formatLabel: value => labelFor(value, mode),
    ...(mode === 'live' && live ? { now: live.now, windowS: live.windowS } : {}),
  });
  options.plugins.tooltip.callbacks.label = item => {
    const ds = datasets[item.datasetIndex];
    const tipUnit = ds?.unit || chart.unit;
    return ' ' + (ds?.label || '') + ': ' + formatValue(item.parsed.y, tipUnit, ds?.scale);
  };
  const constructor: unknown = Reflect.get(window, 'Chart');
  if (typeof constructor !== 'function') throw new Error(sharedMessage('chart_unavailable'));
  const Chart = constructor as ChartConstructor;
  return new Chart(canvas, {
    type: 'line',
    data: { datasets },
    options,
  });
}

function scaleKey(scope: string, mode: string, id: string): string {
  return scope + '\0' + mode + '\0' + id;
}

function rememberMax(scales: Scales, scope: string, mode: string, id: string, yMax: number | undefined): void {
  if (yMax === undefined || !Number.isFinite(yMax)) return;
  scales.set(scaleKey(scope, mode, id), yMax);
}

function rememberedMax(scales: Scales, scope: string, mode: string, id: string): number {
  const held = scales?.get(scaleKey(scope, mode, id));
  return held !== undefined && Number.isFinite(held) ? held : 0;
}

function cardFrame(chart: ChartSpec) {
  const card = document.createElement('article');
  card.className = 'charts-card';
  card.dataset.chartId = chart.id;
  const label = document.createElement('div');
  label.className = 'charts-card-label';
  label.textContent = t(chart.titleKey);
  const value = document.createElement('div');
  value.className = 'charts-card-value';
  const plot = document.createElement('div');
  plot.className = 'charts-plot';
  card.append(label, value, plot);
  return { card, value, plot };
}

function destroyCharts(map: ChartMap): void {
  for (const chart of map.values()) chart.destroy();
  map.clear();
}

function renderGroup(mount: HTMLElement, charts: ChartSpec[], rows: Row[], scope: string, mode: string, map: ChartMap, scales: Scales, live?: Live): void {
  destroyCharts(map);
  mount.replaceChildren();
  const colors = chartColors();
  for (const spec of charts) {
    const { card, value, plot } = cardFrame(spec);
    const datasets = buildDatasets(spec, rows, scope, mode, colors, rememberedMax(scales, scope, mode, spec.id), live);
    const primary = datasets.find(ds => ds.yAxisID !== 'y1');
    rememberMax(scales, scope, mode, spec.id, primary?.yMax);
    applyReadout(value, spec, datasets);
    const canvas = document.createElement('canvas');
    const empty = document.createElement('div');
    empty.className = 'charts-empty';
    empty.textContent = t('no_data');
    const hasData = chartHasData(datasets);
    canvas.hidden = !hasData;
    empty.hidden = hasData;
    plot.append(canvas, empty);
    map.set(spec.id, makeChart(canvas, spec, datasets, mode, live));
    mount.append(card);
  }
}

function updateGroup(charts: ChartSpec[], rows: Row[], scope: string, mode: string, map: ChartMap, scales: Scales, live?: Live): boolean {
  const liveMode = mode === 'live' && live !== undefined && Number.isFinite(live.now);
  for (const spec of charts) {
    const instance = map.get(spec.id);
    if (!instance) return false;
    if (spec.series.length !== instance.data.datasets.length) return false;
    const raw = spec.series.map(series => seriesPoints(rows, scope, series));
    const bounds = seriesBounds(spec, rows, raw, liveMode ? live.now : undefined, liveMode ? live.windowS : undefined);
    const held = rememberedMax(scales, scope, mode, spec.id);
    const range = liveMode
      ? windowedRange(spec.unit, bounds.min, bounds.max, held, true)
      : { min: 0, max: axisMax(spec.unit, bounds.max, held) };
    rememberMax(scales, scope, mode, spec.id, range.max);
    const bytes = spec.unit === 'bytes' || spec.unit === 'bytes_per_s';
    const scale = bytes ? axisBytes(range.max) : null;
    const y = instance.options.scales?.y;
    if (y) {
      y.min = range.min;
      y.max = range.max;
      y.grace = 0;
    }
    if (liveMode) {
      const x = instance.options.scales?.x;
      if (x) {
        x.min = live.now - live.windowS;
        x.max = live.now;
      }
    }
    const tick = instance.options.scales?.y?.ticks;
    if (tick) tick.callback = value => formatValue(value, spec.unit, scale);
    const tooltip = instance.options.plugins?.tooltip?.callbacks;
    if (tooltip) {
      tooltip.label = item => {
        const ds = instance.data.datasets[item.datasetIndex];
        const tipUnit = ds?.unit || spec.unit;
        return ' ' + item.dataset.label + ': ' + formatValue(item.parsed.y, tipUnit, ds?.scale);
      };
    }
    const latest = rows[rows.length - 1];
    const keepFrom = rows[0]?.ts ?? null;
    const radius = pointRadiusFor(rows.length);
    const built = spec.series.map((series, index) => ({
      key: series.key,
      label: t(series.labelKey),
      unit: series.unit || spec.unit,
      yMax: range.max,
      yMin: range.min,
      scale: series.axis === 'y1' ? null : scale,
      pointRadius: radius,
      point: latest ? { x: latest.ts, y: raw[index]?.[rows.length - 1] ?? null } : null,
    }));
    built.forEach((ds, i) => {
      const target = instance.data.datasets[i];
      if (!target) return;
      target.label = ds.label;
      target.scale = ds.scale;
      target.yMax = ds.yMax;
      target.yMin = ds.yMin;
      target.unit = ds.unit;
      target.pointRadius = ds.pointRadius;
      if (liveMode && ds.point) {
        if (keepFrom != null) shiftTo(target, keepFrom);
        appendPoint(target, ds.point);
      } else {
        target.data = rows.map((row, rowIndex) => ({ x: row.ts, y: raw[i]?.[rowIndex] ?? null }));
      }
    });
    const duration = liveMode && !reducedMotion() && rows.length > 1 ? live.duration : 0;
    const animation = instance.options.animation;
    if (animation && animation.x && animation.y) {
      animation.x.duration = duration;
      animation.y.duration = duration;
    }
    instance.update(duration > 0 ? undefined : 'none');
    const plot = instance.canvas?.parentElement;
    const empty = plot?.querySelector<HTMLElement>('.charts-empty');
    const hasData = chartHasData(instance.data.datasets);
    const wasHidden = instance.canvas.hidden;
    instance.canvas.hidden = !hasData;
    if (hasData && wasHidden) instance.resize();
    if (empty) empty.hidden = hasData;
    applyReadout(mountOf(spec.id), spec, instance.data.datasets);
  }
  return true;
}

function mountOf(id: string): HTMLElement | null {
  const root = document.getElementById('chartsMount');
  if (!root) return null;
  return root.querySelector<HTMLElement>(`[data-chart-id="${CSS.escape(id)}"] .charts-card-value`);
}

return { destroyCharts, renderGroup, updateGroup };
}

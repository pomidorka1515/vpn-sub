import { readSeries } from './series.js';
import { chartColors, seriesColor, seriesFill, baseOptions } from './theme.js';

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

function axisBytes(max) {
  if (!Number.isFinite(max) || max <= 0) return { div: 1, unit: 'B' };
  const i = Math.min(Math.floor(Math.log(max) / Math.log(1024)), BYTE_UNITS.length - 1);
  return { div: 1024 ** i, unit: BYTE_UNITS[i] };
}

function formatValue(value, unit, scale) {
  if (value == null || Number.isNaN(value)) return t('na');
  if (unit === 'bytes' || unit === 'bytes_per_s') {
    const suffix = unit === 'bytes_per_s' ? '/s' : '';
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

function thresholdClass(kind, value) {
  if (value == null || kind == null) return '';
  if (kind === 'cpu') return value > 80 ? 'err' : value > 50 ? 'warn' : 'ok';
  if (kind === 'memory') return value > 85 ? 'err' : value > 65 ? 'warn' : 'ok';
  if (kind === 'disk') return value > 90 ? 'err' : value > 75 ? 'warn' : 'ok';
  return '';
}

function lastValue(points) {
  for (let i = points.length - 1; i >= 0; i--) {
    if (points[i] != null) return points[i];
  }
  return null;
}

function sourceOf(row, scope) {
  if (!row) return null;
  if (scope === 'host') return row.host ?? null;
  const panels = row.panels;
  if (!panels || typeof panels !== 'object') return null;
  return Object.prototype.hasOwnProperty.call(panels, scope) ? panels[scope] : null;
}

function seriesPoints(rows, scope, spec) {
  return rows.map(row => readSeries(sourceOf(row, scope), spec));
}

function labelFor(ts, mode) {
  const d = new Date(ts * 1000);
  if (mode === 'snapshot') {
    return d.toLocaleDateString(window.lang === 'ru' ? 'ru-RU' : 'en-US', {
      timeZone: 'UTC', month: 'short', day: 'numeric',
    });
  }
  return d.toLocaleTimeString(window.lang === 'ru' ? 'ru-RU' : 'en-US', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function chartHasData(datasets) {
  return datasets.some(ds => ds.data.some(point => point != null && point.y != null));
}

function applyReadout(el, chart, datasets) {
  if (!el) return;
  const readout = readoutText(chart, datasets);
  el.textContent = readout.text;
  el.className = 'charts-card-value' + (readout.cls ? ' ' + readout.cls : '');
}

function readoutText(chart, datasets) {
  const primary = chart.series.find(s => s.axis !== 'y1') || chart.series[0];
  const ds = datasets.find(d => d.key === primary.key) || datasets[0];
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

function buildDatasets(chart, rows, scope, mode, colors) {
  const raw = chart.series.map(spec => seriesPoints(rows, scope, spec));
  const numeric = raw.flatMap((values, index) => (
    chart.series[index].axis === 'y1' ? [] : values.filter(v => v != null)
  ));
  const max = numeric.length ? Math.max(...numeric) : 0;
  const bytes = chart.unit === 'bytes' || chart.unit === 'bytes_per_s';
  const scale = bytes ? axisBytes(max) : null;
  return chart.series.map((spec, index) => {
    const color = seriesColor(index, colors);
    const values = raw[index];
    return {
      key: spec.key,
      label: t(spec.labelKey),
      labelKey: spec.labelKey,
      unit: spec.unit || chart.unit,
      scale: spec.axis === 'y1' ? null : scale,
      data: rows.map((row, i) => ({ x: row.ts, y: values[i] })),
      borderColor: color,
      backgroundColor: seriesFill(color),
      yAxisID: spec.axis === 'y1' ? 'y1' : 'y',
      fill: spec.axis === 'y1' ? false : 'origin',
      tension: 0.25,
      spanGaps: false,
      pointRadius: rows.length > (window.matchMedia('(max-width: 600px)').matches ? 16 : 24) ? 0 : 2,
      pointHoverRadius: 3,
      borderWidth: 1.5,
    };
  });
}

function syncDataset(target, ds) {
  target.data = ds.data;
  target.label = ds.label;
  target.scale = ds.scale;
  target.unit = ds.unit;
  target.pointRadius = ds.pointRadius;
}

function makeChart(canvas, chart, datasets, mode) {
  const colors = chartColors();
  const scale = datasets.find(ds => ds.yAxisID !== 'y1' && ds.scale)?.scale || null;
  const formatTick = value => {
    return formatValue(value, chart.unit, scale);
  };
  const options = baseOptions(colors, {
    live: mode === 'live',
    beginAtZero: chart.beginAtZero,
    dual: chart.series.some(s => s.axis === 'y1'),
    unit: chart.unit,
    formatTick,
    formatTooltip: (value, tipUnit) => formatValue(value, tipUnit),
    formatLabel: value => labelFor(value, mode),
  });
  options.plugins.tooltip.callbacks.label = item => {
    const ds = datasets[item.datasetIndex] || {};
    const tipUnit = ds.unit || chart.unit;
    return ' ' + (ds.label || '') + ': ' + formatValue(item.parsed.y, tipUnit, ds.scale);
  };
  return new Chart(canvas, {
    type: 'line',
    data: { datasets },
    options,
  });
}

function cardFrame(chart) {
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

export function destroyCharts(map) {
  for (const chart of map.values()) chart.destroy();
  map.clear();
}

export function renderGroup(mount, charts, rows, scope, mode, map) {
  destroyCharts(map);
  mount.replaceChildren();
  const colors = chartColors();
  for (const spec of charts) {
    const { card, value, plot } = cardFrame(spec);
    const datasets = buildDatasets(spec, rows, scope, mode, colors);
    applyReadout(value, spec, datasets);
    const canvas = document.createElement('canvas');
    const empty = document.createElement('div');
    empty.className = 'charts-empty';
    empty.textContent = t('no_data');
    const hasData = chartHasData(datasets);
    canvas.hidden = !hasData;
    empty.hidden = hasData;
    plot.append(canvas, empty);
    map.set(spec.id, makeChart(canvas, spec, datasets, mode));
    mount.append(card);
  }
}

export function updateGroup(charts, rows, scope, mode, map) {
  for (const spec of charts) {
    const instance = map.get(spec.id);
    if (!instance) return false;
    const datasets = buildDatasets(spec, rows, scope, mode, chartColors());
    if (datasets.length !== instance.data.datasets.length) return false;
    const scale = datasets.find(ds => ds.yAxisID !== 'y1' && ds.scale)?.scale || null;
    const tick = instance.options.scales?.y?.ticks;
    if (tick) tick.callback = value => formatValue(value, spec.unit, scale);
    const tooltip = instance.options.plugins?.tooltip?.callbacks;
    if (tooltip) {
      tooltip.label = item => {
        const ds = datasets[item.datasetIndex] || {};
        const tipUnit = ds.unit || spec.unit;
        return ' ' + item.dataset.label + ': ' + formatValue(item.parsed.y, tipUnit, ds.scale);
      };
    }
    datasets.forEach((ds, i) => {
      const target = instance.data.datasets[i];
      if (!target) return;
      syncDataset(target, ds);
    });
    instance.update('none');
    const plot = instance.canvas?.parentElement;
    const empty = plot?.querySelector('.charts-empty');
    const hasData = chartHasData(datasets);
    const wasHidden = instance.canvas.hidden;
    instance.canvas.hidden = !hasData;
    if (hasData && wasHidden) instance.resize();
    if (empty) empty.hidden = hasData;
    applyReadout(mountOf(spec.id), spec, datasets);
  }
  return true;
}

function mountOf(id) {
  const root = document.getElementById('chartsMount');
  if (!root) return null;
  return root.querySelector(`[data-chart-id="${CSS.escape(id)}"] .charts-card-value`);
}

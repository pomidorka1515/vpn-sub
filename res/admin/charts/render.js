import { readSeries } from './series.js';
import { axisMax, windowedRange } from './scale.js';
import { chartColors, seriesColor, seriesFill, baseOptions, reducedMotion } from './theme.js';

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

function pointRadiusFor(count) {
  return count > (window.matchMedia('(max-width: 600px)').matches ? 16 : 24) ? 0 : 2;
}

function windowedYs(rows, values, now, windowS) {
  const inWindow = Number.isFinite(now) && Number.isFinite(windowS);
  const ys = [];
  for (let i = 0; i < values.length; i++) {
    const y = values[i];
    if (y == null || !Number.isFinite(y)) continue;
    if (inWindow) {
      const x = rows[i]?.ts;
      if (!Number.isFinite(x) || x < now - windowS || x > now) continue;
    }
    ys.push(y);
  }
  return ys;
}

function boundsOf(ys) {
  if (!ys.length) return { min: 0, max: 0 };
  return { min: Math.min(...ys), max: Math.max(...ys) };
}

function seriesBounds(spec, rows, raw, now, windowS) {
  const ys = [];
  spec.series.forEach((series, index) => {
    if (series.axis === 'y1') return;
    ys.push(...windowedYs(rows, raw[index], now, windowS));
  });
  return boundsOf(ys);
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

function buildDatasets(chart, rows, scope, mode, colors, heldMax, live) {
  const raw = chart.series.map(spec => seriesPoints(rows, scope, spec));
  const liveOn = mode === 'live' && live && Number.isFinite(live.now);
  const bounds = seriesBounds(chart, rows, raw, liveOn ? live.now : undefined, liveOn ? live.windowS : undefined);
  const range = liveOn
    ? windowedRange(chart.unit, bounds.min, bounds.max, heldMax, true)
    : { min: 0, max: axisMax(chart.unit, bounds.max, heldMax) };
  const bytes = chart.unit === 'bytes' || chart.unit === 'bytes_per_s';
  const scale = bytes ? axisBytes(range.max) : null;
  const radius = pointRadiusFor(rows.length);
  return chart.series.map((spec, index) => {
    const color = seriesColor(index, colors);
    const values = raw[index];
    return {
      key: spec.key,
      label: t(spec.labelKey),
      labelKey: spec.labelKey,
      unit: spec.unit || chart.unit,
      yMax: range.max,
      yMin: range.min,
      scale: spec.axis === 'y1' ? null : scale,
      data: rows.map((row, i) => ({ x: row.ts, y: values[i] })),
      borderColor: color,
      backgroundColor: seriesFill(color),
      yAxisID: spec.axis === 'y1' ? 'y1' : 'y',
      fill: spec.axis === 'y1' ? false : 'origin',
      tension: 0.25,
      spanGaps: false,
      pointRadius: radius,
      pointHoverRadius: 3,
      borderWidth: 1.5,
    };
  });
}

function appendPoint(target, point) {
  const data = target.data;
  const last = data[data.length - 1];
  if (last && last.x === point.x) {
    last.y = point.y;
    return;
  }
  data.push({ x: point.x, y: point.y });
}

function shiftTo(target, x) {
  const data = target.data;
  while (data.length && data[0].x < x) data.shift();
}

function makeChart(canvas, chart, datasets, mode, live) {
  const colors = chartColors();
  const scale = datasets.find(ds => ds.yAxisID !== 'y1' && ds.scale)?.scale || null;
  const formatTick = value => {
    return formatValue(value, chart.unit, scale);
  };
  const options = baseOptions(colors, {
    live: mode === 'live',
    yMax: datasets.find(ds => ds.yAxisID !== 'y1')?.yMax,
    yMin: datasets.find(ds => ds.yAxisID !== 'y1')?.yMin,
    dual: chart.series.some(s => s.axis === 'y1'),
    unit: chart.unit,
    formatTick,
    formatTooltip: (value, tipUnit) => formatValue(value, tipUnit),
    formatLabel: value => labelFor(value, mode),
    now: mode === 'live' ? live?.now : undefined,
    windowS: mode === 'live' ? live?.windowS : undefined,
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

function scaleKey(scope, mode, id) {
  return scope + '\0' + mode + '\0' + id;
}

function rememberMax(scales, scope, mode, id, yMax) {
  if (!scales || !Number.isFinite(yMax)) return;
  scales.set(scaleKey(scope, mode, id), yMax);
}

function rememberedMax(scales, scope, mode, id) {
  const held = scales?.get(scaleKey(scope, mode, id));
  return Number.isFinite(held) ? held : 0;
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

export function renderGroup(mount, charts, rows, scope, mode, map, scales, live) {
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

export function updateGroup(charts, rows, scope, mode, map, scales, live) {
  const liveMode = mode === 'live' && live && Number.isFinite(live.now);
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
        const ds = instance.data.datasets[item.datasetIndex] || {};
        const tipUnit = ds.unit || spec.unit;
        return ' ' + item.dataset.label + ': ' + formatValue(item.parsed.y, tipUnit, ds.scale);
      };
    }
    const latest = rows[rows.length - 1];
    const keepFrom = rows.length ? rows[0].ts : null;
    const radius = pointRadiusFor(rows.length);
    const built = spec.series.map((series, index) => ({
      key: series.key,
      label: t(series.labelKey),
      unit: series.unit || spec.unit,
      yMax: range.max,
      yMin: range.min,
      scale: series.axis === 'y1' ? null : scale,
      pointRadius: radius,
      point: latest ? { x: latest.ts, y: raw[index][raw[index].length - 1] } : null,
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
        target.data = rows.map((row, rowIndex) => ({ x: row.ts, y: raw[i][rowIndex] }));
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
    const empty = plot?.querySelector('.charts-empty');
    const hasData = chartHasData(instance.data.datasets);
    const wasHidden = instance.canvas.hidden;
    instance.canvas.hidden = !hasData;
    if (hasData && wasHidden) instance.resize();
    if (empty) empty.hidden = hasData;
    applyReadout(mountOf(spec.id), spec, instance.data.datasets);
  }
  return true;
}

function mountOf(id) {
  const root = document.getElementById('chartsMount');
  if (!root) return null;
  return root.querySelector(`[data-chart-id="${CSS.escape(id)}"] .charts-card-value`);
}

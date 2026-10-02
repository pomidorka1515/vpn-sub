// Chart.js defaults. Colors are read from CSS variables on each build so a
// theme toggle cannot reuse a cached palette.

const PALETTE_VARS = [
  ['--green', '#22c55e'],
  ['--orange', '#f59e0b'],
  ['--cyan', '#06b6d4'],
  ['--purple', '#a855f7'],
  ['--yellow', '#eab308'],
];

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function withAlpha(color, alpha) {
  if (color.startsWith('#')) {
    const hex = color.slice(1);
    const full = hex.length === 3 ? hex.split('').map(c => c + c).join('') : hex;
    const n = parseInt(full, 16);
    if (Number.isNaN(n) || full.length !== 6) return color;
    const r = (n >> 16) & 255;
    const g = (n >> 8) & 255;
    const b = n & 255;
    return `rgba(${r},${g},${b},${alpha})`;
  }
  return color;
}

export function chartColors() {
  return {
    text: cssVar('--text', '#e4e4e7'),
    text2: cssVar('--text2', '#8e8e96'),
    border: cssVar('--border', '#252529'),
    grid: cssVar('--grid', 'rgba(255,255,255,0.04)'),
    surface: cssVar('--surface', '#131316'),
    surface2: cssVar('--surface2', '#1a1a1f'),
    ok: cssVar('--green', '#22c55e'),
    warn: cssVar('--orange', '#f59e0b'),
    err: cssVar('--red', '#ef4444'),
    palette: PALETTE_VARS.map(([name, fallback]) => cssVar(name, fallback)),
  };
}

export function seriesColor(index, colors) {
  const palette = colors?.palette || PALETTE_VARS.map(([, fallback]) => fallback);
  return palette[index % palette.length];
}

export function seriesFill(color) {
  return withAlpha(color, 0.16);
}

function narrow() {
  return window.matchMedia('(max-width: 600px)').matches;
}

function tickFont() {
  return { family: 'JetBrains Mono', size: narrow() ? 9 : 10 };
}

function axisBorder(colors) {
  return { display: false, dash: [4, 4], color: colors.grid };
}

function dashedGrid(colors) {
  return { color: colors.grid, borderDash: [4, 4] };
}

export function baseOptions(colors, { live, yMax, yMin, dual, unit, formatTick, formatTooltip, formatLabel, now, windowS }) {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const y = {
    min: Number.isFinite(yMin) ? yMin : 0,
    max: yMax,
    grace: 0,
    grid: dashedGrid(colors),
    border: axisBorder(colors),
    ticks: {
      color: colors.text2,
      font: tickFont(),
      maxTicksLimit: narrow() ? 4 : 5,
      callback: formatTick,
    },
  };
  const scales = {
    x: {
      type: 'linear',
      grid: Object.assign(dashedGrid(colors), { drawOnChartArea: true }),
      border: axisBorder(colors),
      ticks: {
        color: colors.text2,
        font: tickFont(),
        maxRotation: 0,
        autoSkip: true,
        maxTicksLimit: narrow() ? 4 : 6,
        callback: function (value, index, ticks) {
          const ts = ticks && ticks[index] ? ticks[index].value : value;
          return formatLabel ? formatLabel(ts) : ts;
        },
      },
    },
    y,
  };
  if (live && Number.isFinite(now) && Number.isFinite(windowS)) {
    scales.x.min = now - windowS;
    scales.x.max = now;
  }
  if (dual) {
    scales.y1 = {
      position: 'right',
      beginAtZero: true,
      min: 0,
      max: 100,
      grid: { display: false },
      ticks: {
        color: colors.text2,
        font: tickFont(),
        maxTicksLimit: narrow() ? 3 : 5,
        callback: v => v + '%',
      },
      border: axisBorder(colors),
    };
  }
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: live && !reduced ? {
      x: { duration: 0, easing: 'linear' },
      y: { duration: 0, easing: 'easeOutQuad' },
    } : { duration: live ? 0 : 250 },
    interaction: { mode: 'index', intersect: false },
    scales,
    plugins: {
      legend: {
        display: true,
        position: 'bottom',
        labels: {
          color: colors.text,
          font: { family: 'Outfit', size: 11 },
          boxWidth: 8,
          boxHeight: 8,
          padding: narrow() ? 8 : 10,
          usePointStyle: true,
          pointStyle: 'circle',
        },
      },
      tooltip: {
        backgroundColor: colors.surface2,
        borderColor: colors.border,
        borderWidth: 1,
        titleColor: colors.text,
        bodyColor: colors.text,
        titleFont: { family: 'Outfit', size: 12, weight: '600' },
        bodyFont: { family: 'JetBrains Mono', size: 11 },
        padding: narrow() ? 6 : 8,
        cornerRadius: 6,
        callbacks: {
          title: items => formatLabel ? formatLabel(items[0]?.parsed?.x) : '',
          label: item => ' ' + item.dataset.label + ': ' + formatTooltip(item.parsed.y, item.dataset.unit || unit),
        },
      },
    },
  };
}

export function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

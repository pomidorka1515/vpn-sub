// Chart.js defaults. Colors are read from CSS variables on each build so a
// theme toggle cannot reuse a cached palette.

const PALETTE_VARS: ReadonlyArray<readonly [string, string]> = [
  ['--green', '#22c55e'],
  ['--orange', '#f59e0b'],
  ['--cyan', '#06b6d4'],
  ['--purple', '#a855f7'],
  ['--yellow', '#eab308'],
];

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function withAlpha(color: string, alpha: number): string {
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

export type ChartColors = ReturnType<typeof chartColors>;

export function seriesColor(index: number, colors?: ChartColors): string {
  const palette = colors?.palette || PALETTE_VARS.map(([, fallback]) => fallback);
  return palette[index % palette.length] || '#22c55e';
}

export function seriesFill(color: string): string {
  return withAlpha(color, 0.16);
}

function narrow(): boolean {
  return window.matchMedia('(max-width: 600px)').matches;
}

function tickFont() {
  return { family: 'JetBrains Mono', size: narrow() ? 9 : 10 };
}

function axisBorder(colors: ChartColors) {
  return { display: false, dash: [4, 4], color: colors.grid };
}

function dashedGrid(colors: ChartColors) {
  return { color: colors.grid, borderDash: [4, 4] };
}

interface BaseOptions {
  live: boolean;
  yMax: number;
  yMin: number;
  dual: boolean;
  unit: string;
  formatTick: (value: number) => string;
  formatTooltip: (value: number, unit: string) => string;
  formatLabel?: (value: number) => string;
  now?: number;
  windowS?: number;
}
interface Tick { value: number }
interface TooltipItem { datasetIndex: number; parsed: { x: number; y: number }; dataset: { label: string; unit?: string } }
export function baseOptions(colors: ChartColors, { live, yMax, yMin, dual, unit, formatTick, formatTooltip, formatLabel, now, windowS }: BaseOptions) {
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
  const x: { type: string; grid: ReturnType<typeof dashedGrid> & { drawOnChartArea: boolean }; border: ReturnType<typeof axisBorder>; ticks: { color: string; font: ReturnType<typeof tickFont>; maxRotation: number; autoSkip: boolean; maxTicksLimit: number; callback: (value: number, index: number, ticks: Tick[]) => string | number }; min?: number; max?: number } = {
      type: 'linear',
      grid: Object.assign(dashedGrid(colors), { drawOnChartArea: true }),
      border: axisBorder(colors),
      ticks: {
        color: colors.text2,
        font: tickFont(),
        maxRotation: 0,
        autoSkip: true,
        maxTicksLimit: narrow() ? 4 : 6,
        callback: function (value: number, index: number, ticks: Tick[]) {
          const ts = ticks?.[index]?.value ?? value;
          return formatLabel ? formatLabel(ts) : ts;
        },
      },
    };
  const scales: { x: typeof x; y: typeof y; y1?: { position: string; beginAtZero: boolean; min: number; max: number; grid: { display: boolean }; ticks: { color: string; font: ReturnType<typeof tickFont>; maxTicksLimit: number; callback: (value: number) => string }; border: ReturnType<typeof axisBorder> } } = {
    x,
    y,
  };
  if (live && now !== undefined && windowS !== undefined && Number.isFinite(now) && Number.isFinite(windowS)) {
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
          callback: (v: number) => v + '%',
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
          title: (items: TooltipItem[]) => formatLabel && items[0] ? formatLabel(items[0].parsed.x) : '',
          label: (item: TooltipItem) => ' ' + item.dataset.label + ': ' + formatTooltip(item.parsed.y, item.dataset.unit || unit),
        },
      },
    },
  };
}

export function reducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

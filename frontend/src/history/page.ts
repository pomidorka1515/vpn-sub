import { readBootstrap } from '../shared/bootstrap';
import { withBusy } from '../shared/busy';
import { escapeHtml, loadingHtml as loading } from '../shared/dom';
import { fmtBytes as bytes } from '../shared/formatting';
import { createI18n } from '../shared/i18n';
import { createNotifications } from '../shared/notifications';
import { createLoadGeneration } from '../shared/stale';
import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';
import { fmtDay, selectSamples } from './data';
import type { Sample } from './data';
import { createHistoryService } from './service';

interface TooltipItem { dataset: { label: string }; parsed: { y: number } }
interface ChartSample { ts: number; down?: number; up?: number; wl_down?: number; wl_up?: number }
interface ChartInstance {
  data: { datasets: { label: string }[] };
  options: { scales: { x: { ticks: { color: string }; border: { color: string } }; y: { grid: { color: string }; ticks: { color: string } } }; plugins: { legend: { labels: { color: string } }; tooltip: { backgroundColor: string; borderColor: string; titleColor: string; bodyColor: string } } };
  update(mode: string): void; destroy(): void;
}
interface ChartWindow extends Window { Chart?: new (ctx: CanvasRenderingContext2D | null, config: object) => ChartInstance }

export function initHistory(doc: Document = document, browser: Window = window): () => void {
  const document = doc;
  const win: ChartWindow = browser;
  const bootstrap = readBootstrap(doc);
  const local = safeStorage(() => win.localStorage);
  const i18n = createI18n(bootstrap, doc, local, safeStorage(() => win.sessionStorage), win.location);
  const { t } = i18n;
  const theme = createTheme(doc, local);
  const notifications = createNotifications(doc, t, win.setTimeout.bind(win), win.clearTimeout.bind(win));
  const toast = (message: string, type = 'success') => { if (!disposed) notifications.toast(message, type); };
  const service = createHistoryService({ baseUrl: bootstrap.prefix + '/webapi', onUnauthorized: () => { if (!disposed) win.location.href = bootstrap.prefix + '/auth'; }, translate: t, toast, request: win.fetch.bind(win) });
  const loadGen = createLoadGeneration();
  const fmtBytes = (value: number) => bytes(value, t);
  const loadingHtml = () => loading(t);
  const abort = new AbortController();
  const timers = new Set<number>();
  let disposed = false;
  function schedule(fn: () => void, delay: number): number { const id = win.setTimeout(() => { timers.delete(id); if (!disposed) fn(); }, delay); timers.add(id); return id; }
  function cancelTimer(id: number | undefined): void { if (id !== undefined) { win.clearTimeout(id); timers.delete(id); } }
  function el(id: string): HTMLElement { const node = doc.getElementById(id); if (!node) throw new Error('Missing element #' + id); return node; }
  function input(id: string): HTMLInputElement { const node = el(id); if (!(node instanceof HTMLInputElement)) throw new Error('Missing input #' + id); return node; }
  function button(id: string): HTMLButtonElement { const node = el(id); if (!(node instanceof HTMLButtonElement)) throw new Error('Missing button #' + id); return node; }
  function canvas(id: string): HTMLCanvasElement { const node = el(id); if (!(node instanceof HTMLCanvasElement)) throw new Error('Missing canvas #' + id); return node; }
  function handleChartJsError() {
    toast(t('chart_load_err'), 'error');
    for (const prefix of ['reg', 'wl']) {
      el(prefix + 'Wrap').style.display = 'none';
      const empty = el(prefix + 'Empty');
      empty.textContent = t('chart_load_err');
      empty.style.display = '';
    }
  }

  let chartJsPromise: Promise<void> | null = null;
  let chartScript: HTMLScriptElement | undefined;
  let cancelChartLoad: (() => void) | undefined;
  function ensureChartJs() {
    if (typeof win.Chart !== 'undefined') return Promise.resolve();
    if (chartJsPromise) return chartJsPromise;
    chartJsPromise = new Promise<void>((resolve, reject) => {
      const script = document.createElement('script');
      chartScript = script;
      script.src = bootstrap.page.chartUrl;
      script.async = true;
      cancelChartLoad = () => {
        script.onload = null;
        script.onerror = null;
        script.remove();
        reject(new Error('Chart.js load cancelled'));
      };
      script.onload = () => {
        cancelChartLoad = undefined;
        chartScript = undefined;
        if (typeof win.Chart === 'undefined') {
          chartJsPromise = null;
          reject(new Error('Chart.js not loaded'));
        } else resolve();
      };
      script.onerror = () => {
        cancelChartLoad = undefined;
        chartScript = undefined;
        chartJsPromise = null;
        reject(new Error('Chart.js not loaded'));
      };
      document.head.appendChild(script);
    });
    return chartJsPromise;
  }

  let regChart: ChartInstance | null = null, wlChart: ChartInstance | null = null;
  let lastData: Sample[] | null = null;


  function cssVar(name: string) {
    return win.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function isNarrow() {
    return win.matchMedia('(max-width: 500px)').matches;
  }

  function chartColors() {
    return {
      text: cssVar('--text'),
      text2: cssVar('--text2'),
      border: cssVar('--border'),
      surface2: cssVar('--surface2'),
      grid: cssVar('--grid'),
      accent: cssVar('--accent'),
      accentLight: cssVar('--accent-light'),
      wl: cssVar('--wl'),
      wlLight: cssVar('--wl-light'),
    };
  }

  function applyChartTheme(chart: ChartInstance | null, colors: ReturnType<typeof chartColors>) {
    if (!chart) return;
    const o = chart.options;
    o.scales.x.ticks.color = colors.text2;
    o.scales.x.border.color = colors.border;
    o.scales.y.grid.color = colors.grid;
    o.scales.y.ticks.color = colors.text2;
    o.plugins.legend.labels.color = colors.text;
    o.plugins.tooltip.backgroundColor = colors.surface2;
    o.plugins.tooltip.borderColor = colors.border;
    o.plugins.tooltip.titleColor = colors.text;
    o.plugins.tooltip.bodyColor = colors.text;
    chart.update('none');
  }

  function repaintCharts() {
    const colors = chartColors();
    applyChartTheme(regChart, colors);
    applyChartTheme(wlChart, colors);
  }

  function makeChart(ctx: CanvasRenderingContext2D | null, data: ChartSample[], downColor: string, upColor: string) {
    if (typeof win.Chart === 'undefined') {
      throw new Error('Chart.js not loaded');
    }
    const c = chartColors();
    const narrow = isNarrow();
    downColor = downColor || c.accent;
    upColor = upColor || c.accentLight;
    const reduceMotion = win.matchMedia('(prefers-reduced-motion: reduce)').matches;
    return new win.Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.map(d => fmtDay(d.ts)),
        datasets: [
          {
            label: t('download'),
            data: data.map(d => d.down !== undefined ? d.down : d.wl_down),
            backgroundColor: downColor,
            borderWidth: 0,
            borderRadius: 2,
            stack: 's',
          },
          {
            label: t('upload'),
            data: data.map(d => d.up !== undefined ? d.up : d.wl_up),
            backgroundColor: upColor,
            borderWidth: 0,
            borderRadius: 2,
            stack: 's',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: reduceMotion ? 0 : 250 },
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: {
              color: c.text2,
              font: { family: 'JetBrains Mono', size: 10 },
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: narrow ? 6 : 12,
            },
            border: { color: c.border },
          },
          y: {
            stacked: true,
            grid: { color: c.grid },
            ticks: {
              color: c.text2,
              font: { family: 'JetBrains Mono', size: 10 },
              callback: (v: number | string) => fmtBytes(Number(v)),
              maxTicksLimit: narrow ? 5 : 8,
            },
            border: { display: false },
          },
        },
        plugins: {
          legend: {
            position: 'bottom',
            labels: {
              color: c.text,
              font: { family: 'Outfit', size: 12 },
              boxWidth: 10, boxHeight: 10, boxPadding: 0,
              padding: narrow ? 10 : 14,
              usePointStyle: true, pointStyle: 'rectRounded',
            },
          },
          tooltip: {
            backgroundColor: c.surface2,
            borderColor: c.border,
            borderWidth: 1,
            titleColor: c.text,
            bodyColor: c.text,
            titleFont: { family: 'Outfit', size: 12, weight: '600' },
            bodyFont: { family: 'JetBrains Mono', size: 11 },
            padding: narrow ? 8 : 10,
            cornerRadius: 6,
            callbacks: {
              label: (c: TooltipItem) => ' ' + c.dataset.label + ': ' + fmtBytes(c.parsed.y),
              footer: (items: TooltipItem[]) => {
                const total = items.reduce((s, it) => s + it.parsed.y, 0);
                return t('total') + ': ' + fmtBytes(total);
              },
            },
          },
        },
      },
    });
  }

  function setLoading(isLoading: boolean) {
    const regWrap = el('regWrap');
    const wlWrap = el('wlWrap');
    const regEmpty = el('regEmpty');
    const wlEmpty = el('wlEmpty');

    if (isLoading) {
      regWrap.style.display = 'none';
      wlWrap.style.display = 'none';
      regEmpty.innerHTML = loadingHtml();
      wlEmpty.innerHTML = loadingHtml();
      regEmpty.style.display = '';
      wlEmpty.style.display = '';
      return;
    }
    if (lastData) renderCharts(lastData);
    else {
      regEmpty.textContent = t('no_data');
      wlEmpty.textContent = t('no_data');
    }
  }

  function renderCharts(filled: Sample[]) {
    const reg = filled.map(d => ({ ts: d.ts, down: d.down, up: d.up }));
    const wl  = filled.map(d => ({ ts: d.ts, wl_down: d.wl_down, wl_up: d.wl_up }));

    const regHasData = reg.some(d => d.down || d.up);
    const wlHasData  = wl.some(d => d.wl_down || d.wl_up);

    const regWrap = el('regWrap');
    const regEmpty = el('regEmpty');
    const wlWrap = el('wlWrap');
    const wlEmpty = el('wlEmpty');

    regWrap.style.display = regHasData ? '' : 'none';
    regEmpty.textContent = t('no_data');
    regEmpty.style.display = regHasData ? 'none' : '';
    wlWrap.style.display = wlHasData ? '' : 'none';
    wlEmpty.textContent = t('no_data');
    wlEmpty.style.display = wlHasData ? 'none' : '';

    if (regChart) { regChart.destroy(); regChart = null; }
    if (wlChart)  { wlChart.destroy();  wlChart  = null; }

    try {
      if (regHasData) {
        regChart = makeChart(
          canvas('regChart').getContext('2d'),
          reg, cssVar('--accent'), cssVar('--accent-light')
        );
      }
      if (wlHasData) {
        wlChart = makeChart(
          canvas('wlChart').getContext('2d'),
          wl, cssVar('--wl'), cssVar('--wl-light')
        );
      }
    } catch (e) {
      handleChartJsError();
    }
  }

  function renderSummaries(filled: Sample[]) {
    let regUp = 0, regDown = 0, wlUp = 0, wlDown = 0;
    for (const d of filled) {
      regUp += d.up; regDown += d.down;
      wlUp += d.wl_up; wlDown += d.wl_down;
    }
    const sumHtml = (up: number, down: number) =>
      '↑ <span class="up-val">' + fmtBytes(up) + '</span>' +
      ' <span class="sep">·</span> ↓ <span class="down-val">' + fmtBytes(down) + '</span>' +
      ' <span class="sep">·</span> ' + escapeHtml(t('total')) + ' ' + fmtBytes(up + down);
    el('regSummary').innerHTML = sumHtml(regUp, regDown);
    el('wlSummary').innerHTML = sumHtml(wlUp, wlDown);
  }

  let resizeT: number | undefined;
  let narrowLayout = isNarrow();
  win.addEventListener('resize', () => {
    cancelTimer(resizeT);
    resizeT = schedule(() => {
      const next = isNarrow();
      if (next === narrowLayout || !lastData) return;
      narrowLayout = next;
      renderCharts(lastData);
    }, 200);
  }, { signal: abort.signal });

  async function loadHistory() {
    const btnRefresh = button('btnRefresh');
    const current = loadGen('history');
    await withBusy(btnRefresh, async () => {
      const days = parseInt(input('daysRange').value, 10);
      setLoading(true);
      const [{ response: res, samples }] = await Promise.all([
        service.history(days),
        ensureChartJs().catch(() => null),
      ]);
      if (disposed || !current()) return;
      if (!res || !res.success) {
        if (res) toast(res.msg || t('network_err'), 'error');
        setLoading(false);
        return;
      }
      if (typeof win.Chart === 'undefined') {
        handleChartJsError();
        return;
      }
      if (!samples) { toast(t('invalid_response'), 'error'); setLoading(false); return; }
      const filled = selectSamples(samples, days);
      if (disposed || !current()) return;
      lastData = filled;
      renderCharts(filled);
      renderSummaries(filled);
    });
  }

  let dbT: number | undefined;
  const range = input('daysRange');
  range.setAttribute('aria-label', t('days_range_aria'));
  range.addEventListener('input', () => { loadGen('history'); el('daysValue').textContent = range.value; cancelTimer(dbT); dbT = schedule(() => { void loadHistory(); }, bootstrap.page.debounceMs); }, { signal: abort.signal });
  el('btnBack').addEventListener('click', () => { win.location.href = bootstrap.prefix + '/panel'; }, { signal: abort.signal });
  el('btnRefresh').addEventListener('click', () => { void loadHistory(); }, { signal: abort.signal });
  el('themeBtn').addEventListener('click', theme.toggleTheme, { signal: abort.signal });
  doc.querySelectorAll<HTMLElement>('[data-lang]').forEach(node => node.addEventListener('click', () => i18n.setLang(node.dataset.lang ?? ''), { signal: abort.signal }));
  const removeThemeListener = theme.onChange(repaintCharts);
  theme.applyTheme(); i18n.initLang();
  void loadHistory();
  return () => { disposed = true; loadGen('history'); abort.abort(); timers.forEach(id => win.clearTimeout(id)); cancelChartLoad?.(); chartScript?.remove(); removeThemeListener(); regChart?.destroy(); wlChart?.destroy(); notifications.dispose(); };
}

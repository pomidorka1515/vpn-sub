import { sharedMessage } from '../../shared/messages';
import { isRecord } from '../../shared/json';
import type { AdminContext } from '../context';
import { chartsFor } from './spec';
import { createChartRenderer } from './render';
import type { ChartMap, Live, Row, Scales } from './render';

interface ChartTransport {
  baseUrl(): string;
  headers(): Record<string, string>;
  guard(): boolean;
  on401(): void;
}
export function createCharts(context: AdminContext, config: ChartTransport, chartUrl: string, debounceMs: number, language: string, request: typeof fetch = fetch) {
const { api, t, toast, doc } = context;
const { destroyCharts, renderGroup, updateGroup } = createChartRenderer(t, language);

const LIVE_KEEP = 1800;
const LIVE_WINDOW_S = 90;

const state: {
  mode: 'snapshot' | 'live'; scope: string; group: string; cutoff: number; delay: number;
  rows: Row[]; panels: string[]; opened: boolean; timer: number | null; stream: AbortController | null;
  lastLive: number; inflight: boolean; errorStreak: number; charts: ChartMap; scales: Scales;
  built: boolean; cutoffTimer: number | null; gen: number; hostCpuDropped: boolean; chartJsFailed: boolean;
} = {
  mode: 'snapshot',
  scope: 'host',
  group: 'general',
  cutoff: 7,
  delay: 2,
  rows: [],
  panels: [],
  opened: false,
  timer: null,
  stream: null,
  lastLive: 0,
  inflight: false,
  errorStreak: 0,
  charts: new Map(),
  scales: new Map(),
  built: false,
  cutoffTimer: null,
  gen: 0,
  // First live host CPU sample has no baseline. Keep the drop across tab
  // pauses so a later real 0.0 still plots. Mode switches clear it.
  hostCpuDropped: false,
  chartJsFailed: false,
};

let chartJsPromise: Promise<void> | null = null;

function $(id: string): HTMLElement | null {
  return doc.getElementById(id);
}

function ensureChartJs(): Promise<void> {
  if (!chartLibMissing()) return Promise.resolve();
  if (chartJsPromise) return chartJsPromise;
  chartJsPromise = new Promise<void>((resolve, reject) => {
    const script = doc.createElement('script');
    script.src = chartUrl;
    script.async = true;
    script.onload = () => { resolve(); };
    script.onerror = () => {
      chartJsPromise = null;
      script.remove();
      reject(new Error(sharedMessage('chart_unavailable')));
    };
    doc.head.appendChild(script);
  });
  return chartJsPromise;
}

function chartLibMissing(): boolean {
  return typeof Reflect.get(window, 'Chart') !== 'function';
}

function failChartLib() {
  state.chartJsFailed = true;
  stopLive();
  setStatus('chart_load_err', 'err');
  toast(t('chart_load_err'), 'error');
}

function setStatus(key: string | null, kind?: string): void {
  const el = $('chartsStatus');
  if (!el) return;
  if (!key) {
    el.hidden = true;
    el.textContent = '';
    el.className = 'charts-status';
    return;
  }
  el.hidden = false;
  el.textContent = t(key);
  el.className = 'charts-status' + (kind ? ' ' + kind : '');
}

function stopTimer() {
  if (state.timer != null) {
    clearTimeout(state.timer);
    state.timer = null;
  }
}

function stopLive() {
  stopTimer();
  const controller = state.stream;
  state.stream = null;
  controller?.abort();
}

function delayMs() {
  const n = state.delay;
  const seconds = Number.isFinite(n) ? Math.min(10, Math.max(1, n)) : 2;
  return seconds * 1000;
}

function liveNow() {
  const latest = state.rows[state.rows.length - 1];
  return latest && Number.isFinite(latest.ts) ? latest.ts : Date.now() / 1000;
}

function liveOpts(): Live | undefined {
  if (state.mode !== 'live') return undefined;
  return {
    now: liveNow(),
    windowS: LIVE_WINDOW_S,
    duration: Math.min(delayMs() * 0.85, 700),
  };
}

function scheduleLive() {
  stopTimer();
  if (state.mode !== 'live' || !tabActive() || state.chartJsFailed) return;
  state.timer = window.setTimeout(() => {
    state.timer = null;
    void pollLive();
  }, delayMs());
}

function tabActive() {
  return $('tab-charts')?.classList.contains('active') === true;
}

function press(group: HTMLElement | null, attr: string, value: string): void {
  group?.querySelectorAll('button').forEach(btn => {
    const on = btn.getAttribute(attr) === value;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function syncControls() {
  const live = state.mode === 'live';
  const cutoff = $('chartsCutoffField');
  const delay = $('chartsDelayField');
  if (cutoff) cutoff.hidden = live;
  if (delay) delay.hidden = !live;
  press($('chartsMode'), 'data-mode', state.mode);
  press($('chartsGroup'), 'data-group', state.group);
  const readout = $('chartsDelayValue');
  if (readout) readout.textContent = state.delay.toFixed(1);
}

function panelNames(rows: Row[]): string[] {
  const names: string[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const panels = row.panels;
    if (!panels || typeof panels !== 'object') continue;
    for (const name of Object.keys(panels)) {
      if (seen.has(name)) continue;
      seen.add(name);
      names.push(name);
    }
  }
  return names;
}

function samePanels(next: string[]): boolean {
  return next.length === state.panels.length && next.every((name, i) => name === state.panels[i]);
}

function renderScope() {
  const group = $('chartsScope');
  if (!group) return;
  const keep = group.querySelector('[data-scope="host"]');
  if (keep) group.replaceChildren(keep);
  else group.replaceChildren();
  for (const name of state.panels) {
    const btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'charts-switch-btn';
    btn.setAttribute('data-scope', name);
    btn.textContent = name;
    group.append(btn);
  }
  if (state.scope !== 'host' && !state.panels.includes(state.scope)) state.scope = 'host';
  press(group, 'data-scope', state.scope);
}

function scopeObject(row: Row | undefined): unknown {
  if (!row) return undefined;
  if (state.scope === 'host') return row.host;
  return row.panels ? row.panels[state.scope] : undefined;
}

function panelIsDown() {
  if (state.scope === 'host' || state.mode !== 'live' || !state.rows.length) return false;
  const latest = state.rows[state.rows.length - 1];
  return scopeObject(latest) == null;
}

function dropBaselineCpu(point: Row): void {
  if (state.hostCpuDropped || !isRecord(point.host)) return;
  if (typeof point.host['cpu'] !== 'number' || point.host['cpu'] !== 0) return;
  point.host = Object.assign({}, point.host, { cpu: null });
  state.hostCpuDropped = true;
}

function showCharts() {
  const mount = $('chartsMount');
  if (!mount) return;
  if (chartLibMissing()) return;
  if (panelIsDown()) {
    destroyCharts(state.charts);
    mount.replaceChildren();
    setStatus('panel_down', 'err');
    state.built = false;
    return;
  }
  setStatus(null);
  const specs = chartsFor(state.scope, state.group);
  const canUpdate = state.mode === 'live' && state.built && state.charts.size === specs.length
    && specs.every(spec => state.charts.has(spec.id));
  if (canUpdate) {
    const ok = updateGroup(specs, state.rows, state.scope, state.mode, state.charts, state.scales, liveOpts());
    if (ok) return;
  }
  renderGroup(mount, specs, state.rows, state.scope, state.mode, state.charts, state.scales, liveOpts());
  state.built = true;
}

function rebuild() {
  state.built = false;
  destroyCharts(state.charts);
  showCharts();
}

async function fetchSnapshots() {
  const gen = ++state.gen;
  state.inflight = true;
  setStatus('loading');
  const [res] = await Promise.all([
    api('POST', '/api/state/snapshots', { cutoff: state.cutoff }),
    ensureChartJs().then(() => true).catch(() => false),
  ]);
  state.inflight = false;
  if (gen !== state.gen || state.mode !== 'snapshot') return;
  if (chartLibMissing()) {
    failChartLib();
    return;
  }
  if (!res || !res.success) {
    setStatus('network_err', 'err');
    if (res && res.msg) toast(res.msg, 'error');
    else if (res) toast(t('network_err'), 'error');
    if (state.rows.length) showCharts();
    return;
  }
  const rows: Row[] = Array.isArray(res.obj) ? res.obj.filter((row: unknown): row is Row => isRecord(row) && typeof row['ts'] === 'number' && (row['panels'] == null || isRecord(row['panels']))).slice().reverse() : [];
  state.rows = rows;
  state.panels = panelNames(rows);
  renderScope();
  state.errorStreak = 0;
  state.chartJsFailed = false;
  rebuild();
}

async function pollLive() {
  if (state.mode !== 'live') return;
  if (state.chartJsFailed) return;
  if (state.stream) return;
  if (!tabActive()) return;
  if (chartLibMissing()) return;
  if (config.guard()) return;
  const controller = new AbortController();
  state.stream = controller;
  const gen = state.gen;
  try {
    const response = await request(config.baseUrl() + '/api/state/polling', {
      headers: { ...config.headers(), Accept: 'text/event-stream' },
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (response.status === 401) {
      stopLive();
      config.on401();
      return;
    }
    if (!response.ok || !response.body ||
        !response.headers.get('Content-Type')?.includes('text/event-stream')) {
      throw new Error(sharedMessage('stream_unavailable'));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) throw new Error(sharedMessage('stream_closed'));
        if (controller !== state.stream || gen !== state.gen) return;
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const data = frame.split('\n').filter(line => line.startsWith('data:'))
            .map(line => line.slice(5).trimStart()).join('\n');
          if (data) receiveLive(JSON.parse(data));
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  } catch  {
    if (!controller.signal.aborted && gen === state.gen) {
      state.errorStreak += 1;
      if (state.errorStreak === 1) toast(t('network_err'), 'error');
      setStatus('network_err', 'err');
    }
  } finally {
    controller.abort();
    if (state.stream === controller) {
      state.stream = null;
      scheduleLive();
    }
  }
}

function receiveLive(res: unknown): void {
  if (state.mode !== 'live' || !tabActive()) return;
  if (chartLibMissing()) {
    failChartLib();
    return;
  }
  if (!isRecord(res) || !res['success'] || !isRecord(res['obj'])) {
    state.errorStreak += 1;
    if (state.errorStreak === 1) toast(t('network_err'), 'error');
    setStatus('network_err', 'err');
    return;
  }
  state.errorStreak = 0;
  const now = Date.now();
  if (now - state.lastLive < delayMs()) return;
  state.lastLive = now;
  const point: Row = {
    ts: now / 1000,
    host: res['obj']['host'],
    panels: isRecord(res['obj']['panels']) ? res['obj']['panels'] : {},
  };
  dropBaselineCpu(point);
  state.rows.push(point);
  if (state.rows.length > LIVE_KEEP) state.rows.splice(0, state.rows.length - LIVE_KEEP);
  const panels = panelNames(state.rows);
  if (!samePanels(panels)) {
    state.panels = panels;
    renderScope();
  }
  showCharts();
}

async function startLive() {
  stopLive();
  state.lastLive = 0;
  state.rows = [];
  state.panels = [];
  state.errorStreak = 0;
  state.hostCpuDropped = false;
  state.chartJsFailed = false;
  state.built = false;
  destroyCharts(state.charts);
  $('chartsMount')?.replaceChildren();
  renderScope();
  setStatus('loading');
  const gen = ++state.gen;
  try {
    await ensureChartJs();
  } catch  {
    if (gen !== state.gen) return;
    failChartLib();
    return;
  }
  if (gen !== state.gen || state.mode !== 'live') return;
  await pollLive();
}

function setMode(mode: string | null): void {
  if (mode !== 'snapshot' && mode !== 'live') return;
  if (mode === state.mode && state.opened) return;
  stopLive();
  state.gen += 1;
  state.mode = mode;
  state.rows = [];
  state.panels = [];
  state.hostCpuDropped = false;
  state.chartJsFailed = false;
  state.errorStreak = 0;
  state.built = false;
  destroyCharts(state.charts);
  $('chartsMount')?.replaceChildren();
  renderScope();
  syncControls();
  if (mode === 'live') void startLive();
  else void fetchSnapshots();
}

function setScope(scope: string | null): void {
  if (!scope || scope === state.scope) return;
  state.scope = scope;
  press($('chartsScope'), 'data-scope', scope);
  rebuild();
}

function setGroup(group: string | null): void {
  if (!group || group === state.group) return;
  state.group = group;
  press($('chartsGroup'), 'data-group', group);
  rebuild();
}

function onCutoff() {
  const raw = doc.querySelector<HTMLInputElement>('#chartsCutoff')?.value ?? '';
  const cutoff = parseInt(raw, 10);
  if (isNaN(cutoff) || cutoff < 1) {
    toast(t('cutoff_invalid'), 'error');
    return;
  }
  state.cutoff = cutoff;
  if (state.mode === 'snapshot' && state.opened) void fetchSnapshots();
}

function onDelay() {
  const raw = Number(doc.querySelector<HTMLInputElement>('#chartsDelay')?.value);
  if (!Number.isFinite(raw)) return;
  state.delay = Math.min(10, Math.max(1, raw));
  const readout = $('chartsDelayValue');
  if (readout) readout.textContent = state.delay.toFixed(1);
}

function commitDelay() {
  onDelay();
  state.scales.clear();
}

function bind(signal: AbortSignal): void {
  $('chartsMode')?.addEventListener('click', event => {
    const btn = event.target instanceof Element ? event.target.closest('[data-mode]') : null;
    if (btn) setMode(btn.getAttribute('data-mode'));
  }, { signal });
  $('chartsScope')?.addEventListener('click', event => {
    const btn = event.target instanceof Element ? event.target.closest('[data-scope]') : null;
    if (btn) setScope(btn.getAttribute('data-scope'));
  }, { signal });
  $('chartsGroup')?.addEventListener('click', event => {
    const btn = event.target instanceof Element ? event.target.closest('[data-group]') : null;
    if (btn) setGroup(btn.getAttribute('data-group'));
  }, { signal });
  $('chartsCutoff')?.addEventListener('input', () => {
    clearTimeout(state.cutoffTimer ?? undefined);
    state.cutoffTimer = window.setTimeout(onCutoff, debounceMs || 1000);
  }, { signal });
  $('chartsCutoff')?.addEventListener('change', () => {
    clearTimeout(state.cutoffTimer ?? undefined);
    state.cutoffTimer = null;
    onCutoff();
  }, { signal });
  $('chartsDelay')?.addEventListener('input', onDelay, { signal });
  $('chartsDelay')?.addEventListener('change', commitDelay, { signal });
  window.addEventListener('pagehide', stopLive, { signal });
  window.addEventListener('pageshow', event => {
    if (!event.persisted || !tabActive() || state.mode !== 'live' || state.chartJsFailed) return;
    if (state.inflight) return;
    scheduleLive();
  }, { signal });
}

function chartsStop() {
  stopLive();
  state.gen += 1;
  state.inflight = false;
}

function chartsOnTab(name: string): void {
  if (name !== 'charts') {
    stopLive();
    return;
  }
  if (!state.opened) {
    state.opened = true;
    syncControls();
    void fetchSnapshots();
    return;
  }
  syncControls();
  if (state.chartJsFailed) {
    setStatus('chart_load_err', 'err');
    return;
  }
  if (state.mode === 'live') {
    if (state.rows.length) showCharts();
    void pollLive();
  } else if (!chartLibMissing()) showCharts();
}

function chartsRefreshTheme() {
  if (!tabActive()) return;
  if (!state.rows.length || chartLibMissing()) return;
  rebuild();
}

function initCharts() {
  const controller = new AbortController();
  bind(controller.signal);
  syncControls();
  let narrowLayout = window.matchMedia('(max-width: 600px)').matches;
  let resizeTimer: number | undefined;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      const next = window.matchMedia('(max-width: 600px)').matches;
      if (next === narrowLayout) return;
      narrowLayout = next;
      if (tabActive() && state.rows.length && !chartLibMissing()) rebuild();
    }, 200);
  }, { signal: controller.signal });
  return () => {
    controller.abort();
    clearTimeout(resizeTimer);
    clearTimeout(state.cutoffTimer ?? undefined);
    state.cutoffTimer = null;
    chartsStop();
    destroyCharts(state.charts);
    state.built = false;
  };
}

return { initCharts, chartsStop, chartsOnTab, chartsRefreshTheme };
}

import { chartsFor } from './charts/spec.js';
import { destroyCharts, renderGroup, updateGroup } from './charts/render.js';

const LIVE_CAP = 60;

const state = {
  mode: 'snapshot',
  scope: 'host',
  group: 'general',
  cutoff: 7,
  delay: 2,
  rows: [],
  panels: [],
  opened: false,
  timer: null,
  inflight: false,
  errorStreak: 0,
  charts: new Map(),
  built: false,
  cutoffTimer: null,
  gen: 0,
  // First live host CPU sample has no baseline. Keep the drop across tab
  // pauses so a later real 0.0 still plots. Mode switches clear it.
  hostCpuDropped: false,
  chartJsFailed: false,
};

let chartJsPromise = null;

function $(id) {
  return document.getElementById(id);
}

function ensureChartJs() {
  if (typeof Chart !== 'undefined') return Promise.resolve();
  if (chartJsPromise) return chartJsPromise;
  chartJsPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = window.CHART_JS;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => {
      chartJsPromise = null;
      script.remove();
      reject(new Error('Chart.js not loaded'));
    };
    document.head.appendChild(script);
  });
  return chartJsPromise;
}

function chartLibMissing() {
  return typeof Chart === 'undefined';
}

function failChartLib() {
  state.chartJsFailed = true;
  stopTimer();
  setStatus('chart_load_err', 'err');
  toast(t('chart_load_err'), 'error');
}

function setStatus(key, kind) {
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

function delayMs() {
  const n = Number(state.delay);
  const seconds = Number.isFinite(n) ? Math.min(10, Math.max(1, n)) : 2;
  return seconds * 1000;
}

function scheduleLive() {
  stopTimer();
  if (state.mode !== 'live' || !tabActive() || state.chartJsFailed) return;
  state.timer = setTimeout(() => {
    state.timer = null;
    pollLive();
  }, delayMs());
}

function tabActive() {
  return $('tab-charts')?.classList.contains('active') === true;
}

function press(group, attr, value) {
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
  if (readout) readout.textContent = Number(state.delay).toFixed(1);
}

function panelNames(rows) {
  const names = [];
  const seen = new Set();
  for (const row of rows) {
    const panels = row?.panels;
    if (!panels || typeof panels !== 'object') continue;
    for (const name of Object.keys(panels)) {
      if (seen.has(name)) continue;
      seen.add(name);
      names.push(name);
    }
  }
  return names;
}

function samePanels(next) {
  return next.length === state.panels.length && next.every((name, i) => name === state.panels[i]);
}

function renderScope() {
  const group = $('chartsScope');
  if (!group) return;
  const keep = group.querySelector('[data-scope="host"]');
  if (keep) group.replaceChildren(keep);
  else group.replaceChildren();
  for (const name of state.panels) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'charts-switch-btn';
    btn.setAttribute('data-scope', name);
    btn.textContent = name;
    group.append(btn);
  }
  if (state.scope !== 'host' && !state.panels.includes(state.scope)) state.scope = 'host';
  press(group, 'data-scope', state.scope);
}

function scopeObject(row) {
  if (!row) return undefined;
  if (state.scope === 'host') return row.host;
  return row.panels ? row.panels[state.scope] : undefined;
}

function panelIsDown() {
  if (state.scope === 'host' || state.mode !== 'live' || !state.rows.length) return false;
  const latest = state.rows[state.rows.length - 1];
  return scopeObject(latest) == null;
}

function dropBaselineCpu(point) {
  if (state.hostCpuDropped || !point.host || typeof point.host !== 'object') return;
  if (typeof point.host.cpu !== 'number' || point.host.cpu !== 0) return;
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
    const ok = updateGroup(specs, state.rows, state.scope, state.mode, state.charts);
    if (ok) return;
  }
  renderGroup(mount, specs, state.rows, state.scope, state.mode, state.charts);
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
  const rows = Array.isArray(res.obj) ? res.obj.slice().reverse() : [];
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
  if (state.inflight) {
    scheduleLive();
    return;
  }
  if (!tabActive()) return;
  if (chartLibMissing()) return;
  state.inflight = true;
  const gen = state.gen;
  let res = null;
  let failed = false;
  try {
    res = await api('GET', '/api/state/polling');
  } catch (e) {
    failed = true;
  }
  state.inflight = false;
  if (gen !== state.gen || state.mode !== 'live') return;
  // Keep the chain alive after leaving the tab. chartsOnTab only resumes
  // when no request is in flight, so dropping this response without a
  // timeout stops live mode until the next mode switch.
  if (!tabActive()) {
    scheduleLive();
    return;
  }
  if (chartLibMissing()) {
    failChartLib();
    return;
  }
  if (failed || !res || !res.success || !res.obj) {
    // api() already toasts a thrown fetch. Toast only a response that came back bad.
    state.errorStreak += 1;
    if (!failed && state.errorStreak === 1) toast(t('network_err'), 'error');
    if (!state.rows.length) setStatus('network_err', 'err');
    scheduleLive();
    return;
  }
  state.errorStreak = 0;
  const point = {
    ts: Date.now() / 1000,
    host: res.obj.host,
    panels: res.obj.panels || {},
  };
  dropBaselineCpu(point);
  state.rows.push(point);
  if (state.rows.length > LIVE_CAP) state.rows.splice(0, state.rows.length - LIVE_CAP);
  const panels = panelNames(state.rows);
  if (!samePanels(panels)) {
    state.panels = panels;
    renderScope();
  }
  showCharts();
  scheduleLive();
}

async function startLive() {
  stopTimer();
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
  } catch (e) {
    if (gen !== state.gen) return;
    failChartLib();
    return;
  }
  if (gen !== state.gen || state.mode !== 'live') return;
  await pollLive();
}

function setMode(mode) {
  if (mode !== 'snapshot' && mode !== 'live') return;
  if (mode === state.mode && state.opened) return;
  stopTimer();
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
  if (mode === 'live') startLive();
  else fetchSnapshots();
}

function setScope(scope) {
  if (!scope || scope === state.scope) return;
  state.scope = scope;
  press($('chartsScope'), 'data-scope', scope);
  rebuild();
}

function setGroup(group) {
  if (!group || group === state.group) return;
  state.group = group;
  press($('chartsGroup'), 'data-group', group);
  rebuild();
}

function onCutoff() {
  const raw = $('chartsCutoff')?.value;
  const cutoff = parseInt(raw, 10);
  if (isNaN(cutoff) || cutoff < 1) {
    toast(t('cutoff_invalid'), 'error');
    return;
  }
  state.cutoff = cutoff;
  if (state.mode === 'snapshot' && state.opened) fetchSnapshots();
}

function onDelay() {
  const raw = Number($('chartsDelay')?.value);
  if (!Number.isFinite(raw)) return;
  state.delay = Math.min(10, Math.max(1, raw));
  const readout = $('chartsDelayValue');
  if (readout) readout.textContent = state.delay.toFixed(1);
  if (state.mode === 'live' && tabActive() && !state.chartJsFailed) scheduleLive();
}

function bind() {
  $('chartsMode')?.addEventListener('click', event => {
    const btn = event.target.closest('[data-mode]');
    if (btn) setMode(btn.getAttribute('data-mode'));
  });
  $('chartsScope')?.addEventListener('click', event => {
    const btn = event.target.closest('[data-scope]');
    if (btn) setScope(btn.getAttribute('data-scope'));
  });
  $('chartsGroup')?.addEventListener('click', event => {
    const btn = event.target.closest('[data-group]');
    if (btn) setGroup(btn.getAttribute('data-group'));
  });
  $('chartsCutoff')?.addEventListener('input', () => {
    clearTimeout(state.cutoffTimer);
    state.cutoffTimer = setTimeout(onCutoff, window.DEBOUNCE_MS || 1000);
  });
  $('chartsCutoff')?.addEventListener('change', () => {
    clearTimeout(state.cutoffTimer);
    state.cutoffTimer = null;
    onCutoff();
  });
  $('chartsDelay')?.addEventListener('input', onDelay);
  window.addEventListener('pagehide', stopTimer);
  window.addEventListener('pageshow', event => {
    if (!event.persisted || !tabActive() || state.mode !== 'live' || state.chartJsFailed) return;
    if (state.inflight) return;
    scheduleLive();
  });
}

export function chartsStop() {
  stopTimer();
  state.gen += 1;
  state.inflight = false;
}

export function chartsOnTab(name) {
  if (name !== 'charts') {
    stopTimer();
    return;
  }
  if (!state.opened) {
    state.opened = true;
    syncControls();
    fetchSnapshots();
    return;
  }
  syncControls();
  if (state.chartJsFailed) {
    setStatus('chart_load_err', 'err');
    return;
  }
  if (state.mode === 'live') {
    if (state.inflight) return;
    if (!state.rows.length) pollLive();
    else {
      showCharts();
      scheduleLive();
    }
  } else if (typeof Chart !== 'undefined') showCharts();
}

export function chartsRefreshTheme() {
  if (!tabActive()) return;
  if (!state.rows.length || chartLibMissing()) return;
  rebuild();
}

bind();
syncControls();

let narrowLayout = window.matchMedia('(max-width: 600px)').matches;
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const next = window.matchMedia('(max-width: 600px)').matches;
    if (next === narrowLayout) return;
    narrowLayout = next;
    if (tabActive() && state.rows.length && typeof Chart !== 'undefined') rebuild();
  }, 200);
});

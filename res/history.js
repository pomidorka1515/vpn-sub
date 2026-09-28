function renderLang() {
  applyStaticLang();
  if (lastData) {
    updateChartLabels();
    renderSummaries(lastData);
  }
}

function goBack() {
  window.location.href = window.DASHBOARD_PAGE
}

function handleChartJsError() {
  toast(t('chart_load_err'), 'error');
  document.getElementById('regWrap').innerHTML = '<div class="chart-empty">' + t('chart_load_err') + '</div>';
  document.getElementById('wlWrap').innerHTML = '<div class="chart-empty">' + t('chart_load_err') + '</div>';
}

window.apiConfig = {
  baseUrl: window.API,
  on401: () => { window.location.href = window.AUTH_PAGE; },
  networkError: 'toast',
  networkMessage: () => t('network_err'),
};





function fmtDay(ts) {
  const d = new Date(ts * 1000);
  // short M/D label, UTC to match endpoint's UTC midnights
  return (d.getUTCMonth() + 1) + '/' + d.getUTCDate();
}

// build a contiguous day range (oldest -> newest), filling gaps with zeros
function fillDays(entries, days) {
  const map = new Map();
  for (const e of entries) map.set(e.ts, e);

  const todayUtc = Math.floor(Date.now() / 1000 / 86400) * 86400;
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const ts = todayUtc - i * 86400;
    const e = map.get(ts);
    out.push({
      ts,
      up: e?.up || 0,
      down: e?.down || 0,
      wl_up: e?.wl_up || 0,
      wl_down: e?.wl_down || 0,
    });
  }
  return out;
}

let regChart = null, wlChart = null;
let lastData = null;
let lastDays = 30;

function updateChartLabels() {
  if (regChart) {
    regChart.data.datasets[0].label = t('download');
    regChart.data.datasets[1].label = t('upload');
    regChart.update('none');
  }
  if (wlChart) {
    wlChart.data.datasets[0].label = t('download');
    wlChart.data.datasets[1].label = t('upload');
    wlChart.update('none');
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
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

function applyChartTheme(chart, colors) {
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

function makeChart(ctx, data, downColor, upColor) {
  if (typeof Chart === 'undefined') {
    throw new Error('Chart.js not loaded');
  }
  const c = chartColors();
  downColor = downColor || c.accent;
  upColor = upColor || c.accentLight;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  return new Chart(ctx, {
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
            maxTicksLimit: 12,
          },
          border: { color: c.border },
        },
        y: {
          stacked: true,
          grid: { color: c.grid },
          ticks: {
            color: c.text2,
            font: { family: 'JetBrains Mono', size: 10 },
            callback: v => fmtBytes(v),
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
            boxWidth: 10, boxHeight: 10,
            padding: 14, usePointStyle: true, pointStyle: 'rectRounded',
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
          padding: 10,
          cornerRadius: 6,
          callbacks: {
            label: c => ' ' + c.dataset.label + ': ' + fmtBytes(c.parsed.y),
            footer: items => {
              const total = items.reduce((s, it) => s + it.parsed.y, 0);
              return t('total') + ': ' + fmtBytes(total);
            },
          },
        },
      },
    },
  });
}

function setLoading(isLoading) {
  const regWrap = document.getElementById('regWrap');
  const wlWrap = document.getElementById('wlWrap');
  const regEmpty = document.getElementById('regEmpty');
  const wlEmpty = document.getElementById('wlEmpty');

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

function renderCharts(filled) {
  const reg = filled.map(d => ({ ts: d.ts, down: d.down, up: d.up }));
  const wl  = filled.map(d => ({ ts: d.ts, wl_down: d.wl_down, wl_up: d.wl_up }));

  const regHasData = reg.some(d => d.down || d.up);
  const wlHasData  = wl.some(d => d.wl_down || d.wl_up);

  const regWrap = document.getElementById('regWrap');
  const regEmpty = document.getElementById('regEmpty');
  const wlWrap = document.getElementById('wlWrap');
  const wlEmpty = document.getElementById('wlEmpty');

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
        document.getElementById('regChart').getContext('2d'),
        reg, cssVar('--accent'), cssVar('--accent-light')
      );
    }
    if (wlHasData) {
      wlChart = makeChart(
        document.getElementById('wlChart').getContext('2d'),
        wl, cssVar('--wl'), cssVar('--wl-light')
      );
    }
  } catch (e) {
    handleChartJsError();
  }
}

function renderSummaries(filled) {
  let regUp = 0, regDown = 0, wlUp = 0, wlDown = 0;
  for (const d of filled) {
    regUp += d.up; regDown += d.down;
    wlUp += d.wl_up; wlDown += d.wl_down;
  }
  const sumHtml = (up, down) =>
    '↑ <span class="up-val">' + fmtBytes(up) + '</span>' +
    '   ↓ <span class="down-val">' + fmtBytes(down) + '</span>' +
    '   · ' + t('total') + ' ' + fmtBytes(up + down);
  document.getElementById('regSummary').innerHTML = sumHtml(regUp, regDown);
  document.getElementById('wlSummary').innerHTML = sumHtml(wlUp, wlDown);
}

async function loadHistory() {
  const btnRefresh = document.getElementById('btnRefresh');
  const current = loadGen('history');
  await withBusy(btnRefresh, async () => {
    const days = parseInt(document.getElementById('daysRange').value, 10);
    lastDays = days;
    setLoading(true);
    const res = await api('GET', '/history?days=' + days);
    if (!current()) return;
    if (!res) { setLoading(false); return; }
    if (!res.success) {
      toast(res.msg || t('network_err'), 'error');
      setLoading(false);
      return;
    }
    const filled = fillDays(res.obj || [], days);
    if (!current()) return;
    lastData = filled;
    renderCharts(filled);
    renderSummaries(filled);
  });
}

let dbT;
const rangeEl = document.getElementById('daysRange');
const dValEl = document.getElementById('daysValue');
rangeEl.setAttribute('aria-label', t('days_range_aria'));
rangeEl.addEventListener('input', () => {
  dValEl.textContent = rangeEl.value;
  clearTimeout(dbT);
  dbT = setTimeout(loadHistory, window.DEBOUNCE_MS);
});

const _applyTheme = applyTheme;
applyTheme = function () {
  _applyTheme();
  if (regChart || wlChart) repaintCharts();
};
applyTheme();
initLang();
renderLang();
loadHistory();

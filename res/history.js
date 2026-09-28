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

function makeChart(ctx, data, downColor, upColor) {
  if (typeof Chart === 'undefined') {
    throw new Error('Chart.js not loaded');
  }
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
      animation: { duration: 250 },
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          stacked: true,
          grid: { display: false },
          ticks: {
            color: '#8e8e96',
            font: { family: 'JetBrains Mono', size: 10 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 12,
          },
          border: { color: '#252529' },
        },
        y: {
          stacked: true,
          grid: { color: 'rgba(255,255,255,0.04)' },
          ticks: {
            color: '#8e8e96',
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
            color: '#e4e4e7',
            font: { family: 'Outfit', size: 12 },
            boxWidth: 10, boxHeight: 10,
            padding: 14, usePointStyle: true, pointStyle: 'rectRounded',
          },
        },
        tooltip: {
          backgroundColor: '#1a1a1f',
          borderColor: '#252529',
          borderWidth: 1,
          titleColor: '#e4e4e7',
          bodyColor: '#e4e4e7',
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
    regEmpty.textContent = t('loading');
    wlEmpty.textContent = t('loading');
    regEmpty.style.display = '';
    wlEmpty.style.display = '';
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
        reg, '#6366f1', '#a5b4fc'
      );
    }
    if (wlHasData) {
      wlChart = makeChart(
        document.getElementById('wlChart').getContext('2d'),
        wl, '#0ea5e9', '#7dd3fc'
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
  btnRefresh.disabled = true;
  btnRefresh.setAttribute('aria-busy', 'true');
  const days = parseInt(document.getElementById('daysRange').value, 10);
  lastDays = days;
  setLoading(true);
  const res = await api('GET', '/history?days=' + days);
  if (!res) {
    btnRefresh.disabled = false;
    btnRefresh.setAttribute('aria-busy', 'false');
    return;
  }
  if (!res.success) {
    toast(res.msg || t('network_err'), 'error');
    btnRefresh.disabled = false;
    btnRefresh.setAttribute('aria-busy', 'false');
    return;
  }
  const filled = fillDays(res.obj || [], days);
  lastData = filled;
  renderCharts(filled);
  renderSummaries(filled);
  btnRefresh.disabled = false;
  btnRefresh.setAttribute('aria-busy', 'false');
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

applyTheme();
initLang();
renderLang();
loadHistory();

import { renderPanelCards } from './panels.js';

export async function loadSystemStatus() {
  const result = document.getElementById('systemStatusResult');
  const current = loadGen('system');
  await withBusy(document.getElementById('btnSystemStatus'), () => {
    result.innerHTML = loadingHtml();
    result.className = 'result-box';
    return loadSystemStatusRequest(result, current);
  });
}

async function loadSystemStatusRequest(result, current) {
  const res = await api('GET', '/api/state/system');
  if (!current()) return;
  if (res && res.success) {
    result.innerHTML = renderSystemStatus(res.obj);
    result.className = 'result-box';
  } else {
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
    result.className = 'result-box error';
  }
}

export function renderSystemStatus(s) {
  if (!s) return `<div class="table-empty">${t('no_data')}</div>`;

  const cpuPct = s.cpu != null ? Math.round(s.cpu * 100) / 100 : null;
  const cpuClr = cpuPct !== null ? (cpuPct > 80 ? 'err' : cpuPct > 50 ? 'warn' : 'ok') : 'ok';
  const cpuName = s.cpu_info?.name || t('na');
  const cpuCores = s.cpu_info?.cores ?? t('na');
  const cpuSpeed = s.cpu_info?.mhz_max ? Math.round(s.cpu_info.mhz_max) + ' MHz' : t('na');
  const loads = s.loadavg
    ? [s.loadavg.load_1m, s.loadavg.load_5m, s.loadavg.load_15m].map(l => (l ?? 0).toFixed(2)).join(', ')
    : t('na');

  const ram = s.memory?.ram;
  const memUsed = ram ? fmtBytes(ram.used) : t('na');
  const memTotal = ram ? fmtBytes(ram.total) : t('na');
  const memAvail = ram ? fmtBytes(ram.available) : t('na');
  const memPct = ram && ram.total > 0 ? Math.round(ram.used / ram.total * 100) : null;
  const memClr = memPct !== null ? (memPct > 85 ? 'err' : memPct > 65 ? 'warn' : 'ok') : 'ok';

  const swap = s.memory?.swap;
  const swapUsed = swap ? fmtBytes(swap.used) : t('na');
  const swapTotal = swap ? fmtBytes(swap.total) : t('na');

  const netRecv = s.network ? fmtBytes(s.network.recv) : t('na');
  const netSent = s.network ? fmtBytes(s.network.sent) : t('na');

  const appMem = s.app_memory ? fmtBytes(s.app_memory.ram) : t('na');
  const appSwap = s.app_memory ? fmtBytes(s.app_memory.swap) : t('na');
  const sysUptime = fmtUptime(s.uptime);
  const appUptime = fmtUptime(s.app_uptime);
  const threads = s.app_thread_amount ?? t('na');
  const procs = s.process_count ?? t('na');

  const ipRows = (label, list) => {
    const arr = Array.isArray(list) ? list.filter(Boolean) : [];
    if (!arr.length) return `<div class="panel-metric"><span class="panel-metric-label">${label}</span><span class="panel-metric-value">${t('na')}</span></div>`;
    return arr.map((ip, i) => {
      const lbl = arr.length > 1 ? `${label} ${i + 1}` : label;
      const fs = ip.length > 28 ? '8px' : ip.length > 22 ? '9px' : ip.length > 15 ? '10px' : '11px';
      return `<div class="panel-metric"><span class="panel-metric-label">${lbl}</span><span class="panel-metric-value" style="font-size:${fs}">${escapeHtml(ip)}</span></div>`;
    }).join('');
  };

  return `
    <div class="panel-grid" style="padding-top:0">
      <div class="panel-section">
        <div class="panel-section-label">${t('system')}</div>
        <div class="panel-metric"><span class="panel-metric-label">${t('cpu')}</span><span class="panel-metric-value ${cpuClr}">${cpuPct !== null ? cpuPct + '%' : t('na')}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('cpu_model')}</span><span class="panel-metric-value" style="font-size:${cpuName.length > 20 ? '9px' : '11px'}">${escapeHtml(cpuName)}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('cores')}</span><span class="panel-metric-value">${cpuCores}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('speed')}</span><span class="panel-metric-value">${cpuSpeed}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('load_avg')}</span><span class="panel-metric-value">${loads}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('processes')}</span><span class="panel-metric-value">${procs}</span></div>
      </div>
      <div class="panel-section">
        <div class="panel-section-label">${t('memory')}</div>
        <div class="panel-metric"><span class="panel-metric-label">${t('used')}</span><span class="panel-metric-value ${memClr}">${memUsed} <span class="unit">/ ${memTotal}</span></span></div>
        ${memPct !== null ? `<div class="panel-metric"><span class="panel-metric-label">${t('percent')}</span><span class="panel-metric-value">${memPct}%</span></div>` : ''}
        <div class="panel-metric"><span class="panel-metric-label">${t('available')}</span><span class="panel-metric-value">${memAvail}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('swap')}</span><span class="panel-metric-value">${swapUsed} <span class="unit">/ ${swapTotal}</span></span></div>
      </div>
      <div class="panel-section">
        <div class="panel-section-label">${t('network')}</div>
        <div class="panel-metric"><span class="panel-metric-label">${t('total_in')}</span><span class="panel-metric-value">${netRecv}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('total_out')}</span><span class="panel-metric-value">${netSent}</span></div>
      </div>
      <div class="panel-section">
        <div class="panel-section-label">${t('connections')}</div>
        <div class="panel-metric"><span class="panel-metric-label">${t('tcp')}</span><span class="panel-metric-value">${s.connections?.tcp ?? t('na')}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('udp')}</span><span class="panel-metric-value">${s.connections?.udp ?? t('na')}</span></div>
      </div>
      <div class="panel-section">
        <div class="panel-section-label">${t('public_ip')}</div>
        ${ipRows(t('ipv4'), s.ip?.ipv4)}
        ${ipRows(t('ipv6'), s.ip?.ipv6)}
      </div>
      <div class="panel-section">
        <div class="panel-section-label">${t('uptime_app')}</div>
        <div class="panel-metric"><span class="panel-metric-label">${t('sys_uptime')}</span><span class="panel-metric-value">${sysUptime}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('app_uptime')}</span><span class="panel-metric-value">${appUptime}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('app_mem')}</span><span class="panel-metric-value">${appMem}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('app_swap')}</span><span class="panel-metric-value">${appSwap}</span></div>
        <div class="panel-metric"><span class="panel-metric-label">${t('app_threads')}</span><span class="panel-metric-value">${threads}</span></div>
      </div>
    </div>
  `;
}

export async function loadSnapshots() {
  const list = document.getElementById('snapshotsList');
  const cutoff = parseInt(document.getElementById('snapCutoff').value, 10);
  if (isNaN(cutoff) || cutoff <= 0) { toast(t('cutoff_invalid'), 'error'); return; }
  const current = loadGen('snapshots');
  await withBusy(document.getElementById('btnLoadSnapshots'), () => {
    list.innerHTML = loadingHtml();
    return loadSnapshotsRequest(list, cutoff, current);
  });
}

async function loadSnapshotsRequest(list, cutoff, current) {
  const res = await api('POST', '/api/state/snapshots', { cutoff });
  if (!current()) return;
  if (!res || !res.success) {
    list.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
    return;
  }
  list.innerHTML = renderSnapshots(res.obj || []);
}

export function fmtSnapDate(ts) {
  if (ts == null || isNaN(ts)) return t('na');
  return new Date(ts * 1000).toLocaleString(window.lang === 'ru' ? 'ru-RU' : 'en-US', {
    timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit'
  }) + ' ' + t('utc');
}

function renderSnapshots(snaps) {
  if (!Array.isArray(snaps) || !snaps.length) {
    return `<div class="table-empty">${t('no_snapshots')}</div>`;
  }
  return `
    <div class="panel-cards">
      ${snaps.map((snap, snapIndex) => {
        const key = 'snap-' + snapIndex;
        const panelNames = snap.panels ? Object.keys(snap.panels) : [];
        const wrappedPanels = Object.fromEntries(panelNames.map(n => [n, { obj: snap.panels[n] }]));
        return `
          <div class="panel-card" id="pc-${key}">
            <div class="panel-card-header" onclick="togglePanelCard('${key}')">
              <div class="panel-card-title">
                <span class="panel-card-name">${escapeHtml(fmtSnapDate(snap.ts))}</span>
              </div>
              <div class="panel-card-meta">
                <span style="font-size:12px;color:var(--text2)">${panelNames.length} ${t('panels_label')}</span>
                <svg class="panel-card-arrow" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="4,6 8,10 12,6"/>
                </svg>
              </div>
            </div>
            <div class="panel-card-body">
              <div class="panel-section-label" style="margin:16px 0 4px">${t('host_system_status')}</div>
              ${snap.host ? renderSystemStatus(snap.host) : `<div class="table-empty">${t('no_data')}</div>`}
              <div class="panel-section-label" style="margin:20px 0 4px">${t('panel_status')}</div>
              ${renderPanelCards(wrappedPanels, key + '-')}
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}


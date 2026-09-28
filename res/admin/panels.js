export async function checkPanelStatus() {
  const name = document.getElementById('panelName').value.trim();
  const path = name ? `/api/panel/status?name=${encodeURIComponent(name)}` : '/api/panel/status';
  const res = await api('GET', path);
  const result = document.getElementById('panelStatusResult');
  if (res && res.success) {
    result.innerHTML = renderPanelCards(res.obj);
    result.className = 'result-box';
  } else {
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
    result.className = 'result-box error';
  }
}

export function renderPanelCards(panels, idPrefix = '') {
  if (!panels || !Object.keys(panels).length) {
    return `<div class="table-empty">${t('no_panels')}</div>`;
  }
  return `
    <div class="panel-cards">
      ${Object.entries(panels).map(([panelName, panel], panelIndex) => {
        const s = panel.obj;
        const cpuPct = s ? Math.round(s.cpu * 100) / 100 : null;
        const cpuClr = cpuPct !== null ? (cpuPct > 80 ? 'err' : cpuPct > 50 ? 'warn' : 'ok') : 'ok';
        const xrayState = s?.xray?.state;
        const xrayClr = xrayState === 'running' ? 'ok' : 'err';
        const xrayVersion = s?.xray?.version || t('na');
        const xrayErr = s?.xray?.errorMsg;

        const uptimeFmt = s ? fmtUptime(s.uptime) : t('na');
        const memUsed = s ? fmtBytes(s.mem.current) : t('na');
        const memTotal = s ? fmtBytes(s.mem.total) : t('na');
        const memPct = s && s.mem.total > 0 ? Math.round(s.mem.current / s.mem.total * 100) : null;
        const memClr = memPct !== null ? (memPct > 85 ? 'err' : memPct > 65 ? 'warn' : 'ok') : 'ok';

        const swapUsed = s ? fmtBytes(s.swap.current) : t('na');
        const swapTotal = s ? fmtBytes(s.swap.total) : t('na');

        const diskUsed = s ? fmtBytes(s.disk.current) : t('na');
        const diskTotal = s ? fmtBytes(s.disk.total) : t('na');
        const diskPct = s && s.disk.total > 0 ? Math.round(s.disk.current / s.disk.total * 100) : null;
        const diskClr = diskPct !== null ? (diskPct > 90 ? 'err' : diskPct > 75 ? 'warn' : 'ok') : 'ok';

        const appMem = s ? fmtBytes(s.appStats.mem) : t('na');
        const netDown = s ? fmtBytes(s.netIO.down) + '/s' : t('na');
        const netUp = s ? fmtBytes(s.netIO.up) + '/s' : t('na');
        const netRecv = s ? fmtBytes(s.netTraffic.recv) : t('na');
        const netSent = s ? fmtBytes(s.netTraffic.sent) : t('na');
        const ipv4 = s?.publicIP?.ipv4 || t('na');
        const ipv6 = s?.publicIP?.ipv6 || t('na');
        const loads = s?.loads ? s.loads.map(l => l.toFixed(2)).join(', ') : t('na');
        const threads = s?.appStats?.threads ?? t('na');

        const cardKey = idPrefix + 'panel-' + panelIndex;
        return `
          <div class="panel-card" id="pc-${cardKey}">
            <div class="panel-card-header" onclick="togglePanelCard('${cardKey}')">
              <div class="panel-card-title">
                <span class="panel-card-name">${escapeHtml(panelName)}</span>
                <span class="badge ${xrayClr}">${escapeHtml(xrayState || t('unknown'))}</span>
              </div>
              <div class="panel-card-meta">
                ${s ? `<span style="font-size:12px;color:var(--text2)">${t('xray')} ${escapeHtml(xrayVersion)}</span>` : ''}
                <svg class="panel-card-arrow" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="4,6 8,10 12,6"/>
                </svg>
              </div>
            </div>
            <div class="panel-card-body">
              <div class="panel-grid">
                <div class="panel-section">
                  <div class="panel-section-label">${t('system')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('cpu')}</span><span class="panel-metric-value ${cpuClr}">${cpuPct !== null ? cpuPct + '%' : t('na')}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('cores')}</span><span class="panel-metric-value">${s?.cpuCores ?? t('na')}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('speed')}</span><span class="panel-metric-value">${s?.cpuSpeedMhz ? Math.round(s.cpuSpeedMhz) + ' MHz' : t('na')}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('load_avg')}</span><span class="panel-metric-value">${escapeHtml(loads)}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('processes')}</span><span class="panel-metric-value">${escapeHtml(threads)}</span></div>
                </div>
                <div class="panel-section">
                  <div class="panel-section-label">${t('memory')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('used')}</span><span class="panel-metric-value ${memClr}">${memUsed} <span class="unit">/ ${memTotal}</span></span></div>
                  ${memPct !== null ? `<div class="panel-metric"><span class="panel-metric-label">${t('percent')}</span><span class="panel-metric-value">${memPct}%</span></div>` : ''}
                  <div class="panel-metric"><span class="panel-metric-label">${t('swap')}</span><span class="panel-metric-value">${swapUsed} <span class="unit">/ ${swapTotal}</span></span></div>
                </div>
                <div class="panel-section">
                  <div class="panel-section-label">${t('disk')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('used')}</span><span class="panel-metric-value ${diskClr}">${diskUsed} <span class="unit">/ ${diskTotal}</span></span></div>
                  ${diskPct !== null ? `<div class="panel-metric"><span class="panel-metric-label">${t('percent')}</span><span class="panel-metric-value">${diskPct}%</span></div>` : ''}
                </div>
                <div class="panel-section">
                  <div class="panel-section-label">${t('network')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('down')}</span><span class="panel-metric-value">${netDown}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('up')}</span><span class="panel-metric-value">${netUp}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('total_in')}</span><span class="panel-metric-value">${netRecv}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('total_out')}</span><span class="panel-metric-value">${netSent}</span></div>
                </div>
                <div class="panel-section">
                  <div class="panel-section-label">${t('public_ip')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('ipv4')}</span><span class="panel-metric-value" style="font-size:${ipv4.length > 20 ? '9px' : ipv4.length > 15 ? '10px' : '11px'}">${escapeHtml(ipv4)}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('ipv6')}</span><span class="panel-metric-value" style="font-size:${ipv6.length > 28 ? '8px' : ipv6.length > 22 ? '9px' : '10px'}">${escapeHtml(ipv6)}</span></div>
                </div>
                <div class="panel-section">
                  <div class="panel-section-label">${t('connections')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('tcp')}</span><span class="panel-metric-value">${s?.tcpCount ?? t('na')}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('udp')}</span><span class="panel-metric-value">${s?.udpCount ?? t('na')}</span></div>
                </div>
                <div class="panel-section">
                  <div class="panel-section-label">${t('uptime_app')}</div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('sys_uptime')}</span><span class="panel-metric-value">${uptimeFmt}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('app_mem')}</span><span class="panel-metric-value">${appMem}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('app_threads')}</span><span class="panel-metric-value">${s?.appStats?.threads ?? t('na')}</span></div>
                  <div class="panel-metric"><span class="panel-metric-label">${t('xray_ver')}</span><span class="panel-metric-value">${escapeHtml(xrayVersion)}</span></div>
                </div>
                ${xrayErr && xrayErr !== '' ? `
                <div class="panel-section">
                  <div class="panel-section-label">${t('xray_error')}</div>
                  <div class="panel-metric"><span class="panel-metric-label err">${escapeHtml(xrayErr)}</span></div>
                </div>` : ''}
              </div>
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

export function togglePanelCard(name) {
  const card = document.getElementById('pc-' + name);
  if (!card) return;
  card.classList.toggle('open');
}

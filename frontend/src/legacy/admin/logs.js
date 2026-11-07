function runLogs(btn) {
  const other = document.getElementById(btn && btn.id === 'btnRefreshLogs' ? 'btnLoadLogs' : 'btnRefreshLogs');
  if (busyDepth(btn) || busyDepth(other)) return;
  const wrap = document.getElementById('logsTableWrap');
  const current = loadGen('logs');
  wrap.innerHTML = loadingHtml();
  const load = withBusy(btn, () => fetchLogs(wrap, current));
  return other ? withBusy(other, () => load) : load;
}

export function loadLogs() {
  return runLogs(document.getElementById('btnLoadLogs'));
}

export function refreshLogs() {
  return runLogs(document.getElementById('btnRefreshLogs'));
}

async function fetchLogs(wrap, current) {
  const count = document.getElementById('logCount').value || 50;
  const res = await api('GET', `/api/logs/audit?n=${encodeURIComponent(count)}`);
  if (!current()) return;
  if (!res || !res.success) {
    wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
    return;
  }
  if (current()) renderLogsTable(res.obj || []);
}

function renderLogsTable(logs) {
  const wrap = document.getElementById('logsTableWrap');
  if (!logs.length) {
    wrap.innerHTML = `<div class="table-empty">${t('no_logs')}</div>`;
    return;
  }
  wrap.innerHTML = `
    <table class="data-table logs-table">
      <thead><tr>
        <th>${t('timestamp')}</th>
        <th>${t('log_action')}</th>
        <th>${t('info')}</th>
      </tr></thead>
      <tbody>
        ${logs.map(l => `
          <tr>
            <td class="log-ts">${escapeHtml(l.date || new Date(l.ts * 1000).toLocaleString())}</td>
            <td><span class="log-action ${l.action}">${escapeHtml(l.action)}</span></td>
            <td class="log-info">${escapeHtml(JSON.stringify(l.info || {}))}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

export async function loadLogs() {
  const count = document.getElementById('logCount').value || 50;
  const res = await api('GET', `/api/logs/audit?n=${encodeURIComponent(count)}`);
  if (!res || !res.success) return;
  renderLogsTable(res.obj || []);
}

export function refreshLogs() {
  loadLogs();
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

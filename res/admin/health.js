import { fmtSnapDate } from './system.js';

function renderHealthDetails(obj) {
  if (!obj) return '';
  const mem = obj.memory || {};
  const ram = Number.isFinite(mem.ram) ? mem.ram.toFixed(2) + ' MB' : t('na');
  const swap = Number.isFinite(mem.swap) ? mem.swap.toFixed(2) + ' MB' : t('na');
  const row = (label, value, cls) =>
    `<div class="panel-metric"><span class="panel-metric-label">${label}</span><span class="panel-metric-value${cls ? ' ' + cls : ''}">${value}</span></div>`;
  return `<div class="panel-grid" style="padding-top:0">
    <div class="panel-section">
      ${row(t('db'), obj.db ? t('yes') : t('no'), obj.db ? 'ok' : 'err')}
      ${row(t('degraded'), obj.degraded ? t('yes') : t('no'), obj.degraded ? 'warn' : 'ok')}
      ${row(t('app_uptime'), fmtUptime(obj.uptime))}
      ${row(t('app_mem'), ram)}
      ${row(t('app_swap'), swap)}
      ${row(t('app_threads'), obj.threads ?? t('na'))}
    </div>
  </div>`;
}

export async function runHealthCheck() {
  const res = await api('GET', '/api/health');
  const dot = document.getElementById('healthDot');
  const text = document.getElementById('healthText');
  const details = document.getElementById('healthDetails');
  const obj = res?.obj;
  if (res && res.success && !obj?.degraded) {
    dot.className = 'health-dot ok';
    text.textContent = t('health_ok');
    text.style.color = '';
  } else if (res && res.success) {
    dot.className = 'health-dot warn';
    text.textContent = t('health_warn');
    text.style.color = 'var(--orange)';
  } else {
    dot.className = 'health-dot err';
    text.textContent = res?.msg || t('health_err');
    text.style.color = 'var(--red)';
  }
  if (obj) {
    details.style.display = '';
    details.innerHTML = renderHealthDetails(obj);
  }
}

export async function loadOnlineUsers() {
  const keyed = document.getElementById('onlineKeyed').checked;
  const path = keyed ? '/api/user/onlines?keyed=1' : '/api/user/onlines';
  const res = await api('GET', path);
  const wrap = document.getElementById('onlineUsersWrap');
  if (!res || !res.success) {
    wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('error'))}</div>`;
    return;
  }
  const data = res.obj.users;
  const unhealthy = Object.entries(res.obj.panel_health || {})
    .filter(([, status]) => status !== 'ok')
    .map(([name, status]) => `${name}: ${status}`);

  if (unhealthy.length) {
    wrap.innerHTML = `<div class="table-empty">${t('partial_data').replace('{detail}', unhealthy.map(escapeHtml).join(', '))}</div>`;
  } else {
    wrap.innerHTML = '';
  }

  if (keyed) {
    const entries = Object.entries(data);
    if (!entries.length) {
      wrap.innerHTML = `<div class="table-empty">${t('no_online')}</div>`;
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>${t('internal_user')}</th>
          <th style="width:48px;text-align:center"></th>
          <th>${t('ext_username')}</th>
        </tr></thead>
        <tbody>
          ${entries.map(([intUser, extUser]) => `
            <tr>
              <td class="mono">${escapeHtml(intUser)}</td>
              <td style="text-align:center;color:var(--text2);font-size:11px"></td>
              <td class="mono">${escapeHtml(extUser ?? t('na'))}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  } else {
    if (!data.length) {
      wrap.innerHTML = `<div class="table-empty">${t('no_online')}</div>`;
      return;
    }
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>${t('internal_user')}</th>
        </tr></thead>
        <tbody>
          ${data.map(user => `
            <tr>
              <td class="mono">${escapeHtml(user)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }
}

export async function doRefreshUsers() {
  const result = document.getElementById('refreshResult');
  result.innerHTML = `<div class="result-msg">${t('loading')}</div>`;
  result.className = 'result-box';
  const res = await api('GET', '/api/user/refresh');
  if (res && res.success) {
    result.innerHTML = `<div class="result-title ok">${t('refreshed')}</div><div class="result-msg">${escapeHtml(res.msg || '')}</div>`;
    result.className = 'result-box success';
  } else {
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
    result.className = 'result-box error';
  }
}

function renderRollbackMarkers(markers) {
  const rows = Object.entries(markers || {}).flatMap(([kind, users]) =>
    Object.entries(users || {}).map(([user, marker]) => ({ kind, user, ...marker }))
  );
  if (!rows.length) return '';
  return `
    <div class="panel-section-label">${t('rollback_markers')}</div>
    <table class="data-table">
      <thead><tr>
        <th>${t('internal_user')}</th>
        <th>${t('marker_type')}</th>
        <th>${t('timestamp')}</th>
        <th>${t('marker_reason')}</th>
        <th style="width:90px;text-align:center">${t('actions')}</th>
      </tr></thead>
      <tbody>
        ${rows.map(({ kind, user, ts, reason }) => `
          <tr>
            <td class="mono">${escapeHtml(user)}</td>
            <td>${escapeHtml(kind)}</td>
            <td class="mono">${ts ? escapeHtml(fmtSnapDate(ts)) : t('na')}</td>
            <td>${escapeHtml(reason || '')}</td>
            <td style="text-align:center">
              <button class="btn-tiny danger" data-rollback-kind="${escapeHtml(kind)}" data-rollback-user="${escapeHtml(user)}">${t('resolve')}</button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderOperationsStatus(obj) {
  const snapshot = obj?.daily_snapshot_failure;
  const markersHtml = renderRollbackMarkers(obj?.rollback_failures);
  if (!snapshot && !markersHtml) {
    return `<div class="table-empty">${t('no_recovery_issues')}</div>`;
  }
  return `
    ${snapshot ? `
      <div class="panel-section-label">${t('snapshot_failure')}</div>
      <div class="panel-cards">
        <div class="panel-card open">
          <div class="panel-card-body">
            <div class="panel-metric"><span class="panel-metric-label">${t('timestamp')}</span><span class="panel-metric-value">${escapeHtml(fmtSnapDate(snapshot.ts))}</span></div>
            <div class="panel-metric"><span class="panel-metric-label">${t('failed')}</span><span class="panel-metric-value">${escapeHtml(String(snapshot.failed))}</span></div>
            <div class="panel-metric"><span class="panel-metric-label">${t('cutoff')}</span><span class="panel-metric-value">${escapeHtml(String(snapshot.eligible))}</span></div>
          </div>
        </div>
      </div>` : ''}
    ${markersHtml}
  `;
}

export async function loadOperationsStatus() {
  const wrap = document.getElementById('operationsStatusWrap');
  wrap.innerHTML = `<div class="table-empty">${t('loading')}</div>`;
  const res = await api('GET', '/api/operations/status');
  if (!res || !res.success) {
    wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
    return;
  }
  wrap.innerHTML = renderOperationsStatus(res.obj);
}

async function resolveRollbackMarker(kind, user) {
  if (!await confirmDialog(t('confirm_resolve_rollback'), t('confirm'), confirmOpts())) return;
  const res = await api('POST', '/api/operations/rollback/resolve', { kind, user });
  if (res && res.success) {
    toast(t('resolved'), 'success');
    await loadOperationsStatus();
  } else {
    toast(res?.msg || t('network_err'), 'error');
  }
}

export function bindOperations() {
  document.getElementById('operationsStatusWrap').addEventListener('click', e => {
    const button = e.target.closest('[data-rollback-kind]');
    if (!button) return;
    resolveRollbackMarker(button.getAttribute('data-rollback-kind'), button.getAttribute('data-rollback-user'));
  });
}

export async function checkTeapot() {
  const res = await api('GET', '/api/teapot');
  const status = document.getElementById('teapotStatus');
  if (res && res.teapot) {
    status.textContent = t('teapot_yes') + ' 🍵';
    status.style.color = 'var(--orange)';
  } else {
    status.textContent = t('teapot_no');
    status.style.color = 'var(--green)';
  }
}

const confirmOpts = () => ({ okText: t('confirm'), cancelText: t('cancel') });

let baseUrl = '';
let apiToken = '';
let authBlocked = false;
let currentUsers = [];
let selectedUser = null;

function renderLang() {
  applyStaticLang();
}


async function bootstrap() {
  const tokenUrl = location.pathname.replace(/\/+$/, '') + '/token';
  try {
    const res = await fetch(tokenUrl);
    if (res.status === 401) {
      authBlocked = true;
      toast(t('reload_reauth'), 'error');
      return;
    }
    const data = await res.json();
    if (!res.ok || !data.success || !data.obj || !data.obj.token || !data.obj.api_root) {
      authBlocked = true;
      toast(t('reload_reauth'), 'error');
      return;
    }
    apiToken = data.obj.token;
    baseUrl = location.origin + data.obj.api_root;
  } catch (e) {
    authBlocked = true;
    toast(t('network_err'), 'error');
  }
}

window.apiConfig = {
  baseUrl: () => baseUrl,
  headers: () => ({ 'Authorization': apiToken }),
  guard: () => authBlocked || !baseUrl || !apiToken,
  guardMessage: () => t('reload_reauth'),
  on401: () => { authBlocked = true; toast(t('reload_reauth'), 'error'); },
  networkError: 'toast',
  networkMessage: () => t('network_err'),
};



function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector(`.tab[onclick*="${name}"]`)?.classList.add('active');
  document.getElementById('tab-' + name)?.classList.add('active');
}

async function loadUsers() {
  const res = await api('GET', '/api/user/list');
  if (!res || !res.success) return;
  currentUsers = res.obj || [];
  renderUsersTable();
}

function renderUsersTable() {
  const wrap = document.getElementById('usersTableWrap');
  if (!currentUsers.length) {
    wrap.innerHTML = `<div class="table-empty">${t('no_users')}</div>`;
    return;
  }
  wrap.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th>${t('internal_user')}</th>
        <th>${t('actions')}</th>
      </tr></thead>
      <tbody>
        ${currentUsers.map(u => `
          <tr>
            <td class="mono">${escapeHtml(u)}</td>
            <td class="actions">
              <button class="btn-tiny" data-user="${escapeHtml(u)}" data-user-action="view">${t('info')}</button>
              <button class="btn-tiny danger" data-user="${escapeHtml(u)}" data-user-action="delete">${t('delete')}</button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

document.getElementById('usersTableWrap').addEventListener('click', e => { const b=e.target.closest('[data-user-action]'); if(!b)return; const u=b.getAttribute('data-user'); b.dataset.userAction==='view'?viewUser(u):deleteUser(u); });

async function viewUser(username) {
  const res = await api('GET', `/api/user/info?user=${encodeURIComponent(username)}`);
  if (!res || !res.success) return;
  selectedUser = username;
  const s = res.obj;
  document.getElementById('userDetailCard').style.display = 'block';
  document.getElementById('userDetailInfo').innerHTML = `
    <div class="info-row"><span class="info-label">${t('internal_user')}</span><span class="info-value">${escapeHtml(s.username || '—')}</span></div>
    <div class="info-row"><span class="info-label">${t('display_name')}</span><span class="info-value">${escapeHtml(s.displayname || '—')}</span></div>
    <div class="info-row"><span class="info-label">${t('uuid')}</span><span class="info-value">${escapeHtml(s.uuid || '—')}</span></div>
    <div class="info-row"><span class="info-label">${t('token')}</span><span class="info-value">${escapeHtml(s.token || '—')}</span></div>
    <div class="info-row"><span class="info-label">${t('fingerprint')}</span><span class="info-value">${escapeHtml(s.fingerprint || '—')}</span></div>
    <div class="info-row"><span class="info-label">${t('monthly_limit')}</span><span class="info-value">${s.bandwidth?.limit || 0} ${t('gb_unit')}</span></div>
    <div class="info-row"><span class="info-label">${t('wl_limit')}</span><span class="info-value">${s.bandwidth?.wl_limit || 0} ${t('gb_unit')}</span></div>
    <div class="info-row"><span class="info-label">${t('expires')}</span><span class="info-value">${s.time ? new Date(s.time * 1000).toLocaleString() : '∞'}</span></div>
    <div class="info-row"><span class="info-label">${t('status')}</span><span class="info-value">
      <span class="badge ${s.enabled ? 'green' : 'red'}">${s.enabled ? t('active') : t('disabled')}</span>
    </span></div>
  `;
  document.getElementById('userDetailResult').innerHTML = '';
}

function hideUserDetail() {
  document.getElementById('userDetailCard').style.display = 'none';
  selectedUser = null;
}

async function doResetUser() {
  if (!selectedUser) return;
  if (!await confirmDialog(t('confirm_reset_user'), t('confirm'), confirmOpts())) return;
  const res = await api('POST', '/api/user/reset', { user: selectedUser });
  const result = document.getElementById('userDetailResult');
  if (res && res.success) {
    result.innerHTML = `<div class="result-title ok">${t('ok')}</div>
      <div class="result-msg">${t('uuid')}: ${escapeHtml(res.obj.uuid)}<br>${t('token')}: ${escapeHtml(res.obj.token)}</div>`;
    result.className = 'result-box success';
  } else {
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
    result.className = 'result-box error';
  }
}

async function deleteUser(username) {
  if (!await confirmDialog(t('confirm_delete_user'), t('confirm'), confirmOpts())) return;
  const res = await api('POST', '/api/user/delete', { user: username });
  if (res && res.success) {
    toast(t('deleted'), 'success');
    loadUsers();
    hideUserDetail();
  } else {
    toast(res?.msg || t('error'), 'error');
  }
}

async function doDeleteUser() {
  if (selectedUser) await deleteUser(selectedUser);
}

function showAddUser() {
  document.getElementById('addUserCard').style.display = 'block';
  document.getElementById('addUser').value = '';
  document.getElementById('addDisplayName').value = '';
  document.getElementById('addExtUser').value = '';
  document.getElementById('addExtPass').value = '';
  document.getElementById('addFp').value = '';
  document.getElementById('addLimit').value = '0';
  document.getElementById('addWlLimit').value = '5';
  document.getElementById('addExpiry').value = '';
  document.getElementById('addToken').value = '';
  document.getElementById('addUuid').value = '';
  document.getElementById('addUserResult').innerHTML = '';
}

function hideAddUser() {
  document.getElementById('addUserCard').style.display = 'none';
}

async function doAddUser() {
  const body = {};
  const user = document.getElementById('addUser').value.trim();
  const displayname = document.getElementById('addDisplayName').value.trim();
  if (!user) { toast(t('internal_user_required'), 'error'); return; }
  if (!displayname) { toast(t('display_name_required'), 'error'); return; }
  body.user = user;
  body.displayname = displayname;

  const extUser = document.getElementById('addExtUser').value.trim();
  const extPass = document.getElementById('addExtPass').value;
  if (extUser) body.ext_username = extUser;
  if (extPass) body.ext_password = extPass;

  const fp = document.getElementById('addFp').value.trim();
  if (fp) body.fingerprint = fp;

  const limit = parseInt(document.getElementById('addLimit').value) || 0;
  const wlLimit = parseInt(document.getElementById('addWlLimit').value) || 5;
  body.limit = limit;
  body.wl_limit = wlLimit;

  const expiry = document.getElementById('addExpiry').value;
  if (expiry) {
    body.time = Math.floor(new Date(expiry).getTime() / 1000);
  }

  const token = document.getElementById('addToken').value.trim();
  if (token) body.token = token;

  const uuid = document.getElementById('addUuid').value.trim();
  if (uuid) body.userid = uuid;

  const result = document.getElementById('addUserResult');
  const res = await api('POST', '/api/user/add', body);
  if (res && res.success) {
    result.innerHTML = `<div class="result-title ok">${t('created')}</div>`;
    result.className = 'result-box success';
    hideAddUser();
    loadUsers();
  } else {
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
    result.className = 'result-box error';
  }
}

async function loadCodes() {
  const list = document.getElementById('codeList');
  list.innerHTML = `<div class="table-empty">${t('loading')}</div>`;
  const res = await api('GET', '/api/code/list');
  if (!res || !res.success) {
    list.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
    return;
  }
  const names = Array.isArray(res.obj) ? res.obj : [];
  if (!names.length) {
    list.innerHTML = `<div class="table-empty">${t('no_codes')}</div>`;
    return;
  }
  const entries = await Promise.all(names.map(async name => {
    const info = await api('GET', `/api/code/info?code=${encodeURIComponent(name)}`);
    return [name, info && info.success ? info.obj : null];
  }));
  renderCodesList(entries);
}

function renderCodesList(entries) {
  const list = document.getElementById('codeList');
  if (!entries.length) {
    list.innerHTML = `<div class="table-empty">${t('no_codes')}</div>`;
    return;
  }
  list.innerHTML = entries.map(([name, c]) => {
    let badges = '';
    const meta = [];
    if (c) {
      const isRegister = c.action === 'register';
      badges += `<span class="badge ${isRegister ? 'purple' : 'cyan'}">${escapeHtml(c.action || '—')}</span>`;
      badges += c.perma
        ? `<span class="badge green">${t('reusable')}</span>`
        : `<span class="badge gray">${c.uses ?? 1} ${t('uses').toLowerCase()}</span>`;
      if (c.days) meta.push(`<span>${c.days} ${t('days').toLowerCase()}</span>`);
      if (c.gb) meta.push(`<span>${c.gb} ${t('gb')}</span>`);
      if (c.wl_gb) meta.push(`<span>${c.wl_gb} ${t('gb')} ${t('wl')}</span>`);
    }
    return `
      <div class="code-card">
        <div class="code-info">
          <div class="code-name">${escapeHtml(name)}</div>
          <div class="code-meta">${badges}${meta.join('')}</div>
        </div>
        <div class="code-actions">
          <button class="btn-tiny" data-code-action="copy" data-code="${escapeHtml(name)}">${t('copy')}</button>
          <button class="btn-tiny danger" data-code-action="delete" data-code="${escapeHtml(name)}">${t('delete')}</button>
        </div>
      </div>
    `;
  }).join('');
}

// Event delegation so code names with special characters can't break handlers.
function onCodeListClick(e) {
  const btn = e.target.closest('button[data-code-action]');
  if (!btn) return;
  const code = btn.getAttribute('data-code');
  const action = btn.getAttribute('data-code-action');
  if (action === 'copy') copyCode(code);
  else if (action === 'delete') deleteCode(code);
}

function copyCode(code) {
  navigator.clipboard.writeText(code)
    .then(() => toast(t('copied'), 'success'))
    .catch(() => toast(t('copy_failed'), 'error'));
}

async function deleteCode(code) {
  if (!await confirmDialog(t('confirm_delete_code'), t('confirm'), confirmOpts())) return;
  const res = await api('POST', '/api/code/delete', { code });
  if (res && res.success) {
    toast(t('deleted'), 'success');
    loadCodes();
  } else {
    toast(res?.msg || t('error'), 'error');
  }
}

function toggleUsesRow() {
  const perma = document.getElementById('addPerma').checked;
  document.getElementById('addUsesRow').style.display = perma ? 'none' : 'flex';
}

function showAddCode() {
  document.getElementById('addCodeCard').style.display = 'block';
  document.getElementById('addCode').value = '';
  document.getElementById('addAction').value = 'bonus';
  document.getElementById('addDays').value = '0';
  document.getElementById('addGb').value = '0';
  document.getElementById('addWlGb').value = '0';
  document.getElementById('addPerma').checked = false;
  document.getElementById('addUses').value = '1';
  toggleUsesRow();
  const result = document.getElementById('addCodeResult');
  result.style.display = 'none';
  result.innerHTML = '';
  document.getElementById('addCode').focus();
}

function hideAddCode() {
  document.getElementById('addCodeCard').style.display = 'none';
}

async function doAddCode() {
  const code = document.getElementById('addCode').value.trim();
  if (!code) { toast(t('code_required'), 'error'); return; }

  const perma = document.getElementById('addPerma').checked;
  const body = {
    code,
    action: document.getElementById('addAction').value,
    perma,
    days: parseInt(document.getElementById('addDays').value, 10) || 0,
    gb: parseInt(document.getElementById('addGb').value, 10) || 0,
    wl_gb: parseInt(document.getElementById('addWlGb').value, 10) || 0,
  };
  // `uses` is ignored by the API when perma is true, so only send it otherwise.
  if (!perma) {
    const uses = parseInt(document.getElementById('addUses').value, 10);
    if (isNaN(uses) || uses <= 0) { toast(t('uses_invalid'), 'error'); return; }
    body.uses = uses;
  }

  const res = await api('POST', '/api/code/add', body);
  if (res && res.success) {
    toast(t('created'), 'success');
    hideAddCode();
    loadCodes();
  } else {
    const result = document.getElementById('addCodeResult');
    result.style.display = 'block';
    result.className = 'result-box error';
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
  }
}

async function loadLogs() {
  const count = document.getElementById('logCount').value || 50;
  const res = await api('GET', `/api/logs/audit?n=${encodeURIComponent(count)}`);
  if (!res || !res.success) return;
  renderLogsTable(res.obj || []);
}

function refreshLogs() {
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

async function runHealthCheck() {
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

async function checkPanelStatus() {
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

function renderPanelCards(panels, idPrefix = '') {
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

function togglePanelCard(name) {
  const card = document.getElementById('pc-' + name);
  if (!card) return;
  card.classList.toggle('open');
}

async function loadSystemStatus() {
  const result = document.getElementById('systemStatusResult');
  result.innerHTML = `<div class="result-msg">${t('loading')}</div>`;
  result.className = 'result-box';
  const res = await api('GET', '/api/state/system');
  if (res && res.success) {
    result.innerHTML = renderSystemStatus(res.obj);
    result.className = 'result-box';
  } else {
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
    result.className = 'result-box error';
  }
}

function renderSystemStatus(s) {
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

async function loadSnapshots() {
  const list = document.getElementById('snapshotsList');
  const cutoff = parseInt(document.getElementById('snapCutoff').value, 10);
  if (isNaN(cutoff) || cutoff <= 0) { toast(t('cutoff_invalid'), 'error'); return; }
  list.innerHTML = `<div class="table-empty">${t('loading')}</div>`;
  const res = await api('POST', '/api/state/snapshots', { cutoff });
  if (!res || !res.success) {
    list.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
    return;
  }
  list.innerHTML = renderSnapshots(res.obj || []);
}

function fmtSnapDate(ts) {
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
        // Snapshot panels are ServerMetricsObj directly; renderPanelCards() expects { obj: status }.
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



async function loadOnlineUsers() {
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
    .filter(([, state]) => state !== 'ok')
    .map(([name, state]) => `${name}: ${state}`);

  if (unhealthy.length) {
    wrap.innerHTML = `<div class="table-empty">${t('partial_data').replace('{detail}', unhealthy.map(escapeHtml).join(', '))}</div>`;
  } else {
    wrap.innerHTML = '';
  }

  if (keyed) {
    // keyed: { internalUsername: "externalUsername" | null }
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
    // array: ["username"]
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

async function doRefreshUsers() {
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

async function loadOperationsStatus() {
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

async function checkTeapot() {
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

async function fetchLeaderboard() {
  const type = document.getElementById('lbType').value;
  const cutoff = parseInt(document.getElementById('lbCutoff').value) || 0;
  const displaynames = document.getElementById('lbDisplaynames').checked;
  const flip = document.getElementById('lbFlip').checked;

  const body = {
    type,
    cutoff: cutoff > 0 ? cutoff : 0,
    displaynames: !!displaynames,
    flip: !!flip,
  };

  const wrap = document.getElementById('lbTableWrap');
  wrap.innerHTML = `<div class="table-empty" style="padding:20px">${t('loading')}</div>`;

  const res = await api('POST', '/api/leaderboard', body);
  if (!res || !res.success) {
    wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('error'))}</div>`;
    return;
  }

  const data = res.obj;
  if (!Array.isArray(data) || data.length === 0) {
    wrap.innerHTML = `<div class="table-empty">${t('no_entries')}</div>`;
    return;
  }

  wrap.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th style="width:50px">${t('lb_rank')}</th>
        <th>${t('lb_user')}</th>
        <th style="text-align:right">${t('lb_bandwidth')}</th>
      </tr></thead>
      <tbody>
        ${data.map(entry => `
          <tr>
            <td class="mono" style="color:var(--text2)">${entry.place}</td>
            <td class="mono">${escapeHtml(entry.username)}</td>
            <td class="mono" style="text-align:right">${fmtBytes(entry.amount)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

const confirmOpts = () => ({ okText: t('confirm'), cancelText: t('cancel') });

applyTheme();
initLang();
renderLang();
bootstrap();
document.getElementById('codeList').addEventListener('click', onCodeListClick);
document.getElementById('operationsStatusWrap').addEventListener('click', e => {
  const button = e.target.closest('[data-rollback-kind]');
  if (!button) return;
  resolveRollbackMarker(button.getAttribute('data-rollback-kind'), button.getAttribute('data-rollback-user'));
});

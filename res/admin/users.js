import { state } from './state.js';

export async function loadUsers() {
  const wrap = document.getElementById('usersTableWrap');
  const current = loadGen('users');
  await withBusy(document.getElementById('btnLoadUsers'), async () => {
    wrap.innerHTML = loadingHtml();
    const res = await api('GET', '/api/user/list');
    if (!current()) return;
    if (!res || !res.success) {
      wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
      return;
    }
    state.currentUsers = res.obj || [];
    if (current()) renderUsersTable();
  });
}

function renderUsersTable() {
  const wrap = document.getElementById('usersTableWrap');
  if (!state.currentUsers.length) {
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
        ${state.currentUsers.map(u => `
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

export function bindUsersTable() {
  document.getElementById('usersTableWrap').addEventListener('click', e => {
    const b = e.target.closest('[data-user-action]');
    if (!b) return;
    const u = b.getAttribute('data-user');
    b.dataset.userAction === 'view' ? viewUser(u, b) : deleteUser(u, b);
  });
}

export async function viewUser(username, btn) {
  await withBusy(btn, () => viewUserRequest(username));
}

async function viewUserRequest(username) {
  const res = await api('GET', `/api/user/info?user=${encodeURIComponent(username)}`);
  if (!res || !res.success) return;
  state.selectedUser = username;
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

export function hideUserDetail() {
  document.getElementById('userDetailCard').style.display = 'none';
  state.selectedUser = null;
}

export async function doResetUser() {
  if (!state.selectedUser) return;
  if (!await confirmDialog(t('confirm_reset_user'), t('confirm'), confirmOpts())) return;
  await withBusy(document.getElementById('btnDoReset'), async () => {
    const res = await api('POST', '/api/user/reset', { user: state.selectedUser });
    const result = document.getElementById('userDetailResult');
    if (res && res.success) {
      result.innerHTML = `<div class="result-title ok">${t('ok')}</div>
        <div class="result-msg">${t('uuid')}: ${escapeHtml(res.obj.uuid)}<br>${t('token')}: ${escapeHtml(res.obj.token)}</div>`;
      result.className = 'result-box success';
    } else {
      result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
      result.className = 'result-box error';
    }
  });
}

export async function deleteUser(username, btn) {
  if (!await confirmDialog(t('confirm_delete_user'), t('confirm'), confirmOpts())) return;
  const trigger = btn || document.getElementById('btnDoDelete');
  await withBusy(trigger, async () => {
    const res = await api('POST', '/api/user/delete', { user: username });
    if (res && res.success) {
      toast(t('deleted'), 'success');
      loadUsers();
      hideUserDetail();
    } else {
      toast(res?.msg || t('error'), 'error');
    }
  });
}

export async function doDeleteUser() {
  if (state.selectedUser) await deleteUser(state.selectedUser);
}

export function showAddUser() {
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

export function hideAddUser() {
  document.getElementById('addUserCard').style.display = 'none';
}

export async function doAddUser() {
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
  await withBusy(document.getElementById('btnDoAdd'), async () => {
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
  });
}

const confirmOpts = () => ({ okText: t('confirm'), cancelText: t('cancel') });

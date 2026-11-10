import { busyDepth, withBusy } from '../shared/busy';
import { escapeHtml } from '../shared/dom';
import { fmtBytes } from '../shared/formatting';
import { renderIfChanged } from '../shared/render';
import { isRecord } from '../shared/json';
import { ICON_DELETE, ICON_EDIT, ICON_INFO } from '../shared/icons';
import type { AdminState } from './bootstrap';
import { button, checkbox, element, input } from './context';
import type { AdminContext } from './context';
import { USER_FIELD_PERMISSIONS } from './permissions';

export function initUsers({ doc, t, api, toast, confirm, loadGen, can = () => true }: AdminContext, state: AdminState): () => void {
  const listeners = new AbortController();
  const wrap = element(doc, 'usersTableWrap');
  const search = input(doc, 'userSearch');
  const webOnly = checkbox(doc, 'webUsersOnly');
  const externalNames = new Map<string, string>();
  let usersLoaded = false;
  let disposed = false;
  let detailGeneration = 0;
  let resetAvailable = false;
  const getDialog = (id: string): HTMLDialogElement => {
    const dialog = element(doc, id);
    if (!(dialog instanceof HTMLDialogElement)) throw new Error(t('error'));
    return dialog;
  };
  const detail = getDialog('userDetailCard');
  const edit = getDialog('userEditDialog');
  const localDateTime = (date: Date): string => new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  for (const id of ['addExpiry', 'editExpiry']) input(doc, id).setAttribute('max', '9999-12-31T23:59');
  for (const id of ['editUsed', 'editWlUsed']) input(doc, id).addEventListener('input', () => { input(doc, id).setCustomValidity(''); }, { signal: listeners.signal });
  const usageValue = (bandwidth: Record<string, unknown>, exact: string, key: string): string => {
    const value = bandwidth[exact] ?? bandwidth[key];
    return typeof value === 'string' || typeof value === 'number' ? String(value) : '0';
  };
  let editingUser: string | null = null;
  let original: Record<string, unknown> = {};
  let originalExpiry = '';
  let expiryChanged = false;
  let opener: HTMLElement | null = null;
  const actionContent = (key: string, icon: string): string => `${icon}<span class="user-action-label">${escapeHtml(t(key))}</span>`;
  const editButton = button(doc, 'btnEditDetail');
  editButton.innerHTML = actionContent('edit', ICON_EDIT);
  const options = (): { okText: string; cancelText: string } => ({ okText: t('confirm'), cancelText: t('cancel') });
  function hideDetail(): void {
    detailGeneration++;
    resetAvailable = false;
    button(doc, 'btnPasswordReset').disabled = true;
    if (detail.open) detail.close();
    state.selectedUser = null;
  }
  async function loadUsers(): Promise<void> {
    const current = loadGen('users');
    await withBusy(button(doc, 'btnLoadUsers'), async () => {
      const res = await api('GET', '/api/user/list?include_external=1');
      if (disposed || !current()) return;
      if (!res?.success) { toast(res?.msg || t('network_err'), 'error'); return; }
      externalNames.clear();
      state.currentUsers = Array.isArray(res.obj) ? res.obj.flatMap((user: unknown): string[] => {
        if (typeof user === 'string') return [user];
        if (!isRecord(user) || typeof user['username'] !== 'string') return [];
        if (typeof user['ext_username'] === 'string' && user['ext_username'].trim()) externalNames.set(user['username'], user['ext_username']);
        return [user['username']];
      }) : [];
      usersLoaded = true;
      renderUsers();
    });
  }
  function renderUsers(): void {
    if (!usersLoaded) return;
    const query = search.value.trim().toLowerCase();
    const users = state.currentUsers.filter(user => (!webOnly.checked || externalNames.has(user)) && (user.toLowerCase().includes(query) || externalNames.get(user)?.toLowerCase().includes(query)));
    if (!users.length) { renderIfChanged(wrap, `<div class="table-empty">${escapeHtml(t(state.currentUsers.length ? 'no_matching_users' : 'no_users'))}</div>`); return; }
    renderIfChanged(wrap, `<table class="data-table users-table"><thead><tr><th>${t(webOnly.checked ? 'ext_username' : 'internal_user')}</th>${webOnly.checked ? '' : `<th class="user-external">${t('ext_username')}</th>`}<th>${t('actions')}</th></tr></thead><tbody>${users.map(user => `<tr><td class="mono user-name">${escapeHtml(webOnly.checked ? externalNames.get(user) : user)}</td>${webOnly.checked ? '' : `<td class="mono user-external">${escapeHtml(externalNames.get(user) || '—')}</td>`}<td><div class="actions">${([['view', 'info', ICON_INFO], ['edit', 'edit', ICON_EDIT], ['delete', 'delete', ICON_DELETE]] as const).map(([action, key, icon]) => {
      const allowed = state.panelsConfigured && (action === 'view' ? can('user.read') : action === 'edit' ? Object.values(USER_FIELD_PERMISSIONS).some(can) : can('user.delete'));
      const title = allowed ? t(key) : t('permission_missing').replace('{permission}', action === 'edit' ? 'user.edit.*' : 'user.delete');
      return `<button class="btn-tiny user-action user-${action}${action === 'delete' ? ' danger' : ''}" data-user="${escapeHtml(user)}" data-user-action="${action}" aria-label="${escapeHtml(t(key))}" title="${escapeHtml(title)}" ${allowed ? '' : 'disabled'}>${actionContent(key, icon)}</button>`;
    }).join('')}</div></td></tr>`).join('')}</tbody></table>`);
  }
  async function viewUser(username: string, trigger: HTMLButtonElement, editing = false): Promise<void> {
    const generation = ++detailGeneration;
    await withBusy(trigger, async () => {
      const res = await api('GET', `/api/user/info?user=${encodeURIComponent(username)}`);
      if (disposed || generation !== detailGeneration) return;
      if (!res?.success || !isRecord(res.obj)) { toast(res?.msg || t('network_err'), 'error'); return; }
      if (!detail.open && !edit.open) opener = trigger;
      state.selectedUser = username;
      const user = res.obj;
      const bandwidth = isRecord(user['bandwidth']) ? user['bandwidth'] : {};
      if (editing) {
        const fingerprints = await api('GET', '/api/fingerprints');
        if (generation !== detailGeneration) return;
        if (!fingerprints?.success || !Array.isArray(fingerprints.obj) || !fingerprints.obj.length || !fingerprints.obj.every((fp: unknown) => typeof fp === 'string' && fp.length > 0)) {
          toast(fingerprints?.msg || t('network_err'), 'error'); return;
        }
        editingUser = username;
        original = user;
        input(doc, 'editDisplayName').value = typeof user['displayname'] === 'string' ? user['displayname'] : '';
        const external = input(doc, 'editExtUser');
        external.value = typeof user['ext_username'] === 'string' ? user['ext_username'] : '';
        external.disabled = !external.value;
        element(doc, 'editExtHint').hidden = !external.disabled;
        const fp = input(doc, 'editFp');
        const currentFp = typeof user['fingerprint'] === 'string' ? user['fingerprint'] : '';
        fp.replaceChildren();
        if (!fingerprints.obj.includes(currentFp)) {
          const current = doc.createElement('option');
          current.value = currentFp; current.textContent = currentFp || t('na'); current.disabled = true;
          fp.appendChild(current);
        }
        fingerprints.obj.forEach((value: string) => {
          const option = doc.createElement('option');
          option.value = value; option.textContent = value;
          fp.appendChild(option);
        });
        fp.value = currentFp;
        input(doc, 'editLimit').value = String(Number(bandwidth['limit'] ?? 0));
        input(doc, 'editWlLimit').value = String(Number(bandwidth['wl_limit'] ?? 0));
        for (const [id, exact, key] of [['editUsed', 'monthly_exact', 'monthly'], ['editWlUsed', 'wl_monthly_exact', 'wl_monthly']] as const) {
          const field = input(doc, id);
          field.setCustomValidity('');
          field.value = usageValue(bandwidth, exact, key);
        }
        const expiry = typeof user['time'] === 'number' && user['time'] ? new Date(user['time'] * 1000) : null;
        const expiryField = input(doc, 'editExpiry');
        const representable = expiry !== null && Number.isFinite(expiry.getTime()) && expiry.getFullYear() <= 9999;
        expiryField.value = representable ? localDateTime(expiry) : '';
        const expiryHint = expiry && !representable ? t('expiry_out_of_range') : '';
        expiryField.setAttribute('placeholder', expiryHint);
        expiryField.title = expiryHint;
        const rangeHint = element(doc, 'editExpiryRangeHint');
        rangeHint.textContent = expiryHint;
        rangeHint.hidden = !expiryHint;
        originalExpiry = expiryField.value;
        expiryChanged = false;
        for (const [id, field] of [['editDisplayName', 'displayname'], ['editExtUser', 'new_username'], ['editFp', 'fingerprint'], ['editExpiry', 'time'], ['editUsed', 'used'], ['editLimit', 'limit'], ['editWlUsed', 'wl_used'], ['editWlLimit', 'wl_limit']] as const) {
          const target = input(doc, id); const permission = USER_FIELD_PERMISSIONS[field] ?? '*';
          target.disabled = !can(permission) || id === 'editExtUser' && !external.value;
          target.title = can(permission) ? '' : t('permission_missing').replace('{permission}', permission);
        }
        if (!edit.open) edit.showModal();
        return;
      }
      const row = (key: string, value: unknown): string => `<div class="info-row"><span class="info-label">${t(key)}</span><span class="info-value">${escapeHtml(value || '—')}</span></div>`;
      const resetButton = button(doc, 'btnPasswordReset');
      const hasWebAccount = typeof user['ext_username'] === 'string' && user['ext_username'].length > 0;
      resetAvailable = hasWebAccount && can('user.password_reset');
      resetButton.disabled = !resetAvailable || busyDepth(resetButton) > 0;
      element(doc, 'passwordResetUnavailable').hidden = hasWebAccount;
      const trafficRow = (key: string, value: unknown): string => {
        const data = isRecord(value) ? value : {};
        const format = (part: string): string => escapeHtml(fmtBytes(typeof data[part] === 'number' ? data[part] : NaN, t));
        return `<div class="info-row traffic-row"><span class="info-label">${t(key)}</span><span class="info-value">${format('total')}</span><span class="traffic-breakdown">↑ ${format('upload')} / ↓ ${format('download')}</span></div>`;
      };
      const limit = (value: unknown): string => Number(value || 0) === 0 ? '∞' : `${escapeHtml(value)} ${escapeHtml(t('gb_unit'))}`;
      element(doc, 'userDetailInfo').innerHTML = row('internal_user', username) + row('display_name', user['displayname']) + row('uuid', user['uuid']) + row('token', user['token']) + row('fingerprint', user['fingerprint']) +
        trafficRow('total_traffic', bandwidth['total']) + trafficRow('wl_total_traffic', bandwidth['wl_total']) +
        `<div class="info-row"><span class="info-label">${t('monthly_limit')}</span><span class="info-value">${limit(bandwidth['limit'])}</span></div>` +
        `<div class="info-row"><span class="info-label">${t('wl_limit')}</span><span class="info-value">${limit(bandwidth['wl_limit'])}</span></div>` +
        row('expires', typeof user['time'] === 'number' && user['time'] ? (Number.isFinite(new Date(user['time'] * 1000).getTime()) ? new Date(user['time'] * 1000).toLocaleString() : user['time_exact'] ?? t('na')) : '∞') +
        `<div class="info-row"><span class="info-label">${t('status')}</span><span class="info-value"><span class="badge ${user['enabled'] ? 'green' : 'red'}">${user['enabled'] ? t('active') : t('disabled')}</span></span></div>`;
      const result = element(doc, 'userDetailResult');
      result.innerHTML = '';
      result.hidden = true;
      if (!detail.open) detail.showModal();
    });
  }
  async function saveUser(): Promise<void> {
    if (!input(doc, 'editExpiry').reportValidity() || !input(doc, 'editFp').reportValidity()) return;
    const username = editingUser;
    if (!username) return;
    const bandwidth = isRecord(original['bandwidth']) ? original['bandwidth'] : {};
    const body: Record<string, string | number> = { username };
    for (const [id, key, previous] of [['editDisplayName', 'displayname', original['displayname']], ['editExtUser', 'new_username', original['ext_username']], ['editFp', 'fingerprint', original['fingerprint']]] as const) {
      const field = input(doc, id);
      if (!field.disabled && field.value.trim() !== previous) body[key] = field.value.trim();
    }
    for (const [id, key] of [['editLimit', 'limit'], ['editWlLimit', 'wl_limit']] as const) {
      if (input(doc, id).disabled) continue;
      const value = Number(input(doc, id).value);
      if (value !== bandwidth[key]) body[key] = value;
    }
    for (const [id, key, exact, previous] of [['editUsed', 'used', 'monthly_exact', 'monthly'], ['editWlUsed', 'wl_used', 'wl_monthly_exact', 'wl_monthly']] as const) {
      const field = input(doc, id);
      if (field.disabled) continue;
      const valid = /^[0-9]+$/.test(field.value) && BigInt(field.value) <= 9223372036854775807n;
      field.setCustomValidity(valid ? '' : t('used_bytes_invalid'));
      if (!field.reportValidity()) return;
      if (BigInt(field.value) !== BigInt(usageValue(bandwidth, exact, previous))) body[key] = field.value;
    }
    const expiry = input(doc, 'editExpiry').value;
    const time = expiry ? Math.floor(new Date(expiry).getTime() / 1000) : 0;
    // datetime-local has minute precision; preserve untouched timestamps.
    if (!input(doc, 'editExpiry').disabled && (expiry !== originalExpiry || (expiryChanged && !expiry && original['time']))) body['time'] = time;
    await withBusy(button(doc, 'btnSaveUser'), async () => {
      const res = await api('POST', '/api/user/update', body);
      if (disposed) return;
      if (!res?.success) { toast(res?.msg || t('error'), 'error'); return; }
      edit.close();
      toast(t('user_updated'), 'success');
      if (detail.open) void viewUser(username, editButton);
    });
  }
  async function deleteUser(username: string, trigger: HTMLButtonElement): Promise<void> {
    if (!await confirm(t('confirm_delete_user'), t('confirm'), options()) || disposed) return;
    await withBusy(trigger, async () => {
      const res = await api('POST', '/api/user/delete', { user: username });
      if (disposed) return;
      if (res?.success) { toast(t('deleted'), 'success'); void loadUsers(); hideDetail(); }
      else toast(res?.msg || t('error'), 'error');
    });
  }
  async function resetUser(): Promise<void> {
    const username = state.selectedUser;
    if (!username || !await confirm(t('confirm_reset_user') + '\n' + username, t('confirm'), options()) || disposed) return;
    await withBusy(button(doc, 'btnDoReset'), async () => {
      const res = await api('POST', '/api/user/reset', { user: username });
      if (disposed || state.selectedUser !== username) return;
      const result = element(doc, 'userDetailResult');
      if (res?.success && isRecord(res.obj)) {
        result.innerHTML = `<div class="result-title ok">${t('ok')}</div><div class="result-msg">${t('uuid')}: ${escapeHtml(res.obj['uuid'])}<br>${t('token')}: ${escapeHtml(res.obj['token'])}</div>`;
        result.className = 'result-box success';
      } else {
        result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
        result.className = 'result-box error';
      }
      result.hidden = false;
    });
  }
  async function issuePasswordReset(): Promise<void> {
    const username = state.selectedUser;
    if (!username || !resetAvailable) return;
    try { await withBusy(button(doc, 'btnPasswordReset'), async () => {
      const res = await api('POST', '/api/user/password-reset', { user: username });
      if (disposed || state.selectedUser !== username) return;
      if (!res?.success || !isRecord(res.obj) || typeof res.obj['link'] !== 'string') {
        toast(res?.msg || t('error'), 'error'); return;
      }
      const result = element(doc, 'userDetailResult');
      result.replaceChildren();
      const hint = doc.createElement('p');
      hint.className = 'result-msg';
      hint.textContent = t('password_reset_hint');
      const link = doc.createElement('input');
      link.type = 'text'; link.className = 'modal-input'; link.readOnly = true; link.value = res.obj['link'];
      link.setAttribute('aria-label', t('password_reset_link'));
      link.addEventListener('click', () => { link.select(); });
      result.append(hint, link);
      result.className = 'result-box success'; result.hidden = false;
      link.focus(); link.select();
    }); } finally { button(doc, 'btnPasswordReset').disabled = !resetAvailable; }
  }
  function showAddUser(): void {
    element(doc, 'addUserCard').style.display = 'block';
    for (const [id, value] of [['addUser', ''], ['addDisplayName', ''], ['addExtUser', ''], ['addExtPass', ''], ['addFp', ''], ['addLimit', '0'], ['addWlLimit', '5'], ['addExpiry', ''], ['addToken', ''], ['addUuid', '']] as const) input(doc, id).value = value;
    element(doc, 'addUserResult').innerHTML = '';
  }
  function hideAddUser(): void { element(doc, 'addUserCard').style.display = 'none'; }
  async function addUser(): Promise<void> {
    if (!input(doc, 'addExpiry').reportValidity()) return;
    const user = input(doc, 'addUser').value.trim();
    const displayname = input(doc, 'addDisplayName').value.trim();
    if (!user) { toast(t('internal_user_required'), 'error'); return; }
    if (!displayname) { toast(t('display_name_required'), 'error'); return; }
    const body: Record<string, string | number> = { user, displayname };
    for (const [id, key, trim] of [['addExtUser', 'ext_username', true], ['addExtPass', 'ext_password', false], ['addFp', 'fingerprint', true], ['addToken', 'token', true], ['addUuid', 'userid', true]] as const) {
      const raw = input(doc, id).value;
      const value = trim ? raw.trim() : raw;
      if (value) body[key] = value;
    }
    body['limit'] = parseInt(input(doc, 'addLimit').value, 10) || 0;
    const wlLimit = parseInt(input(doc, 'addWlLimit').value, 10);
    body['wl_limit'] = Number.isNaN(wlLimit) ? 5 : wlLimit;
    const expiry = input(doc, 'addExpiry').value;
    if (expiry) body['time'] = Math.floor(new Date(expiry).getTime() / 1000);
    await withBusy(button(doc, 'btnDoAdd'), async () => {
      const res = await api('POST', '/api/user/add', body);
      if (disposed) return;
      const result = element(doc, 'addUserResult');
      if (res?.success) {
        result.innerHTML = `<div class="result-title ok">${t('created')}</div>`; result.className = 'result-box success';
        hideAddUser(); void loadUsers();
      } else {
        result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`; result.className = 'result-box error';
      }
    });
  }
  wrap.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const target = event.target.closest<HTMLButtonElement>('button[data-user-action]');
    if (!target || target.disabled || !wrap.contains(target) || target.dataset['user'] === undefined) return;
    if (target.dataset['userAction'] === 'view') void viewUser(target.dataset['user'], target);
    else if (target.dataset['userAction'] === 'edit') void viewUser(target.dataset['user'], target, true);
    else if (target.dataset['userAction'] === 'delete') void deleteUser(target.dataset['user'], target);
  }, { signal: listeners.signal });
  const bind = (id: string, handler: () => void): void => { button(doc, id).addEventListener('click', handler, { signal: listeners.signal }); };
  bind('btnLoadUsers', () => { void loadUsers(); });
  search.addEventListener('input', renderUsers, { signal: listeners.signal });
  webOnly.addEventListener('change', renderUsers, { signal: listeners.signal });
  bind('btnShowAdd', showAddUser);
  bind('btnCancelAdd', hideAddUser);
  bind('btnDoAdd', () => { void addUser(); });
  bind('btnCloseDetail', hideDetail);
  bind('btnDoReset', () => { void resetUser(); });
  bind('btnPasswordReset', () => { void issuePasswordReset(); });
  bind('btnEditDetail', () => { if (state.selectedUser) void viewUser(state.selectedUser, editButton, true); });
  bind('btnCancelEdit', () => { edit.close(); });
  input(doc, 'editExpiry').addEventListener('input', () => { expiryChanged = true; }, { signal: listeners.signal });
  element(doc, 'userEditForm').addEventListener('submit', event => { event.preventDefault(); void saveUser(); }, { signal: listeners.signal });
  for (const dialog of [detail, edit]) {
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    }, { signal: listeners.signal });
    dialog.addEventListener('close', () => {
      if (dialog === detail) { detailGeneration++; state.selectedUser = null; }
      if (dialog === edit) editingUser = null;
      if (!detail.open && !edit.open && opener?.isConnected) opener.focus();
    }, { signal: listeners.signal });
  }
  bind('btnDoDelete', () => { if (state.selectedUser) void deleteUser(state.selectedUser, button(doc, 'btnDoDelete')); });
  return () => { disposed = true; detailGeneration++; listeners.abort(); if (edit.open) edit.close(); if (detail.open) detail.close(); };
}

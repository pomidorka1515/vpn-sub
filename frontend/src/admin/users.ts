import { withBusy } from '../shared/busy';
import { escapeHtml } from '../shared/dom';
import { renderIfChanged } from '../shared/render';
import { isRecord } from '../shared/json';
import type { AdminState } from './bootstrap';
import { button, element, input } from './context';
import type { AdminContext } from './context';

export function initUsers({ doc, t, api, toast, confirm, loadGen }: AdminContext, state: AdminState): () => void {
  const listeners = new AbortController();
  const wrap = element(doc, 'usersTableWrap');
  let disposed = false;
  let detailGeneration = 0;
  const options = (): { okText: string; cancelText: string } => ({ okText: t('confirm'), cancelText: t('cancel') });
  function hideDetail(): void {
    detailGeneration++;
    element(doc, 'userDetailCard').style.display = 'none';
    state.selectedUser = null;
  }
  async function loadUsers(): Promise<void> {
    const current = loadGen('users');
    await withBusy(button(doc, 'btnLoadUsers'), async () => {
      const res = await api('GET', '/api/user/list');
      if (disposed || !current()) return;
      if (!res?.success) { toast(res?.msg || t('network_err'), 'error'); return; }
      state.currentUsers = Array.isArray(res.obj) ? res.obj.filter((user: unknown): user is string => typeof user === 'string') : [];
      if (!state.currentUsers.length) { renderIfChanged(wrap, `<div class="table-empty">${t('no_users')}</div>`); return; }
      renderIfChanged(wrap, `<table class="data-table"><thead><tr><th>${t('internal_user')}</th><th>${t('actions')}</th></tr></thead><tbody>${state.currentUsers.map(user => `<tr><td class="mono">${escapeHtml(user)}</td><td class="actions"><button class="btn-tiny" data-user="${escapeHtml(user)}" data-user-action="view" ${state.panelsConfigured ? '' : 'disabled'}>${t('info')}</button><button class="btn-tiny danger" data-user="${escapeHtml(user)}" data-user-action="delete" ${state.panelsConfigured ? '' : 'disabled'}>${t('delete')}</button></td></tr>`).join('')}</tbody></table>`);
    });
  }
  async function viewUser(username: string, trigger: HTMLButtonElement): Promise<void> {
    const generation = ++detailGeneration;
    await withBusy(trigger, async () => {
      const res = await api('GET', `/api/user/info?user=${encodeURIComponent(username)}`);
      if (disposed || generation !== detailGeneration || !res?.success || !isRecord(res.obj)) return;
      state.selectedUser = username;
      const user = res.obj;
      const bandwidth = isRecord(user.bandwidth) ? user.bandwidth : {};
      const row = (key: string, value: unknown): string => `<div class="info-row"><span class="info-label">${t(key)}</span><span class="info-value">${escapeHtml(value || '—')}</span></div>`;
      element(doc, 'userDetailCard').style.display = 'block';
      element(doc, 'userDetailInfo').innerHTML = row('internal_user', username) + row('display_name', user.displayname) + row('uuid', user.uuid) + row('token', user.token) + row('fingerprint', user.fingerprint) +
        `<div class="info-row"><span class="info-label">${t('monthly_limit')}</span><span class="info-value">${escapeHtml(bandwidth.limit || 0)} ${t('gb_unit')}</span></div>` +
        `<div class="info-row"><span class="info-label">${t('wl_limit')}</span><span class="info-value">${escapeHtml(bandwidth.wl_limit || 0)} ${t('gb_unit')}</span></div>` +
        row('expires', typeof user.time === 'number' && user.time ? new Date(user.time * 1000).toLocaleString() : '∞') +
        `<div class="info-row"><span class="info-label">${t('status')}</span><span class="info-value"><span class="badge ${user.enabled ? 'green' : 'red'}">${user.enabled ? t('active') : t('disabled')}</span></span></div>`;
      element(doc, 'userDetailResult').innerHTML = '';
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
        result.innerHTML = `<div class="result-title ok">${t('ok')}</div><div class="result-msg">${t('uuid')}: ${escapeHtml(res.obj.uuid)}<br>${t('token')}: ${escapeHtml(res.obj.token)}</div>`;
        result.className = 'result-box success';
      } else {
        result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
        result.className = 'result-box error';
      }
    });
  }
  function showAddUser(): void {
    element(doc, 'addUserCard').style.display = 'block';
    for (const [id, value] of [['addUser', ''], ['addDisplayName', ''], ['addExtUser', ''], ['addExtPass', ''], ['addFp', ''], ['addLimit', '0'], ['addWlLimit', '5'], ['addExpiry', ''], ['addToken', ''], ['addUuid', '']] as const) input(doc, id).value = value;
    element(doc, 'addUserResult').innerHTML = '';
  }
  function hideAddUser(): void { element(doc, 'addUserCard').style.display = 'none'; }
  async function addUser(): Promise<void> {
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
    body.limit = parseInt(input(doc, 'addLimit').value, 10) || 0;
    const wlLimit = parseInt(input(doc, 'addWlLimit').value, 10);
    body.wl_limit = Number.isNaN(wlLimit) ? 5 : wlLimit;
    const expiry = input(doc, 'addExpiry').value;
    if (expiry) body.time = Math.floor(new Date(expiry).getTime() / 1000);
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
    if (!target || !wrap.contains(target) || target.dataset.user === undefined) return;
    if (target.dataset.userAction === 'view') void viewUser(target.dataset.user, target);
    else if (target.dataset.userAction === 'delete') void deleteUser(target.dataset.user, target);
  }, { signal: listeners.signal });
  const bind = (id: string, handler: () => void): void => button(doc, id).addEventListener('click', handler, { signal: listeners.signal });
  bind('btnLoadUsers', () => { void loadUsers(); });
  bind('btnShowAdd', showAddUser);
  bind('btnCancelAdd', hideAddUser);
  bind('btnDoAdd', () => { void addUser(); });
  bind('btnCloseDetail', hideDetail);
  bind('btnDoReset', () => { void resetUser(); });
  bind('btnDoDelete', () => { if (state.selectedUser) void deleteUser(state.selectedUser, button(doc, 'btnDoDelete')); });
  return () => { disposed = true; detailGeneration++; listeners.abort(); };
}

import { isRecord } from '../shared/json';
import type { AdminState } from './bootstrap';
import type { Translate } from '../shared/formatting';

export interface PermissionEntry { permission: string; requires: string | null }
export function allows(grants: readonly string[], permission: string): boolean {
  return grants.some(grant => grant === '*' || grant === permission || (grant.endsWith('.*') && permission.startsWith(grant.slice(0, -1))));
}
export function foldPermissions(selected: ReadonlySet<string>, catalog: readonly PermissionEntry[], mayGrant: (permission: string) => boolean): string[] {
  const leaves = catalog.map(entry => entry.permission);
  if (leaves.length && leaves.every(permission => selected.has(permission)) && mayGrant('*')) return ['*'];
  const branches = new Set(leaves.flatMap(permission => permission.split('.').slice(0, -1).map((_, index) => permission.split('.').slice(0, index + 1).join('.'))));
  const result: string[] = [];
  for (const branch of [...branches].sort((a, b) => a.split('.').length - b.split('.').length || a.localeCompare(b))) {
    const grant = branch + '.*';
    if (!allows(result, grant) && mayGrant(grant) && leaves.filter(permission => permission.startsWith(branch + '.')).every(permission => selected.has(permission))) result.push(grant);
  }
  result.push(...leaves.filter(permission => selected.has(permission) && !allows(result, permission)));
  return result.sort();
}

export const ROOT_CONFIG_KEYS = new Set(['api_token', 'api_admin_ui_auth', 'salt', 'bot', 'publicbot', 'discord', 'redis']);
export const configSection = (key: string): string => key === 'profiles' ? 'inbounds' : key === '3xui' ? 'panels' : 'main';
export const USER_FIELD_PERMISSIONS: Record<string, string> = {
  displayname: 'user.edit.displayname', new_username: 'user.edit.username', fingerprint: 'user.edit.fingerprint', time: 'user.edit.time',
  used: 'user.edit.bandwidth.monthly.used', limit: 'user.edit.bandwidth.monthly.limit', wl_used: 'user.edit.bandwidth.wl_monthly.used', wl_limit: 'user.edit.bandwidth.wl_monthly.limit',
};
const ROUTES: Record<string, string[]> = {
  '/api/user/list': ['user.read'], '/api/user/info': ['user.read'], '/api/user/history': ['user.read'], '/api/fingerprints': ['user.read'],
  '/api/user/add': ['user.read', 'user.create'], '/api/user/delete': ['user.read', 'user.delete'], '/api/user/reset': ['user.read', 'user.reset'],
  '/api/user/password-reset': ['user.read', 'user.password_reset'], '/api/user/update': ['user.read'], '/api/user/refresh': ['health.sync'], '/api/user/onlines': ['health.read.online_users'],
  '/api/code/list': ['code.read'], '/api/code/info': ['code.read'], '/api/code/add': ['code.read'], '/api/code/delete': ['code.read', 'code.delete'],
  '/api/logs/audit': ['audit.read'], '/api/panel/status': ['state.read.panels'], '/api/state/system': ['state.read.host'], '/api/state/all': ['state.read.panels', 'state.read.host'],
  '/api/state/polling': ['state.charts.read'], '/api/state/snapshots': ['snapshots.read'], '/api/health': ['health.read.status'], '/api/teapot': ['health.read.teapot'],
  '/api/operations/status': ['health.read.recovery'], '/api/operations/rollback/resolve': ['*'], '/api/leaderboard': [], '/api/config/get': [], '/api/config/set': [],
  '/api/auth/permissions': [], '/api/auth/list': [], '/api/auth/create': [], '/api/auth/update': [], '/api/auth/delete': [],
};
export function requestPermissions(path: string, body?: unknown): string[] {
  const [route = '', query = ''] = path.split('?');
  const required = [...(ROUTES[route] ?? ['*'])];
  const data = isRecord(body) ? body : {};
  if (route === '/api/user/update') Object.entries(USER_FIELD_PERMISSIONS).forEach(([field, permission]) => { if (Object.hasOwn(data, field)) required.push(permission); });
  if (route === '/api/code/add') required.push('code.add.' + String(data['action']));
  if (route === '/api/leaderboard') required.push('leaderboard.read.' + String(data['type']));
  if (route === '/api/config/set' && isRecord(data['values'])) Object.keys(data['values']).forEach(key => { required.push('config.write.' + configSection(key)); if (ROOT_CONFIG_KEYS.has(key)) required.push('*'); });
  if (route.startsWith('/api/auth/') && route !== '/api/auth/permissions') {
    const kind = route === '/api/auth/list' ? new URLSearchParams(query).get('kind') : data['kind'];
    const action = route.endsWith('/create') ? 'create' : route.endsWith('/list') ? 'read' : 'update';
    required.push('auth.read.' + String(kind), 'auth.' + action + '.' + String(kind));
  }
  return required;
}
export function missingPermission(state: AdminState, path: string, body?: unknown): string | undefined {
  if (path.split('?')[0] === '/api/config/get' && !['main', 'inbounds', 'panels'].some(section => allows(state.permissions, 'config.read.' + section))) return 'config.read.main';
  return requestPermissions(path, body).find(permission => !allows(state.permissions, permission));
}
export const TAB_PERMISSIONS: Record<string, string[]> = {
  auth: ['auth.read.accounts', 'auth.read.tokens'], users: ['user.read'], codes: ['code.read'], logs: ['audit.read'], state: ['state.read.panels', 'state.read.host'],
  snapshots: ['snapshots.read'], charts: ['snapshots.read', 'state.charts.read'], health: ['health.read.recovery', 'health.read.status', 'health.read.online_users', 'health.read.teapot', 'health.sync'],
  leaderboard: ['leaderboard.read.total', 'leaderboard.read.monthly', 'leaderboard.read.wl_monthly'], config: ['config.read.main', 'config.read.inbounds', 'config.read.panels'],
};
export function applyPermissions(doc: Document, state: AdminState, t: Translate): void {
  const can = (permission: string): boolean => allows(state.permissions, permission);
  const needsPanels = new Set(['btnShowAdd', 'btnDoReset', 'btnDoDelete', 'btnDoAdd', 'btnDoRefresh', 'btnLoadOnline', 'onlineKeyed']);
  const restrict = (target: HTMLElement, permissions: string[]): void => {
    const permitted = permissions.some(can);
    const noPanels = !state.panelsConfigured && needsPanels.has(target.id);
    const allowed = permitted && !noPanels;
    target.setAttribute('aria-disabled', String(!allowed));
    target.classList.toggle('permission-disabled', !allowed);
    if (!allowed) target.title = !permitted ? t('permission_missing').replace('{permission}', permissions.join(' / ')) : t('no_panels');
    if (!allowed && (target instanceof HTMLButtonElement || target instanceof HTMLInputElement || target instanceof HTMLOptionElement)) target.disabled = true;
  };
  doc.querySelectorAll<HTMLElement>('[data-tab]').forEach(tab => {
    const permissions = TAB_PERMISSIONS[tab.dataset['tab'] ?? ''] ?? ['*'];
    const allowed = permissions.some(can);
    tab.classList.toggle('permission-disabled', !allowed);
    tab.title = allowed ? '' : t('permission_missing').replace('{permission}', permissions.join(' / '));
    tab.removeAttribute('aria-disabled');
  });
  for (const [name, permissions] of Object.entries(TAB_PERMISSIONS)) {
    const tab = doc.getElementById('tab-' + name);
    if (!tab) continue;
    const restricted = !permissions.some(can);
    tab.classList.toggle('permission-restricted', restricted);
    let notice = tab.querySelector<HTMLElement>(':scope > .permission-tab-notice');
    if (restricted) {
      if (!notice) {
        notice = doc.createElement('div');
        notice.className = 'permission-tab-notice';
        notice.setAttribute('role', 'status');
        tab.prepend(notice);
      }
      notice.textContent = t('permission_missing').replace('{permission}', permissions.join(' / '));
    } else notice?.remove();
    for (const child of tab.children) {
      if (!(child instanceof HTMLElement) || child === notice) continue;
      if (restricted && !child.inert) { child.inert = true; child.dataset['permissionInert'] = ''; }
      else if (!restricted && child.dataset['permissionInert'] !== undefined) { child.inert = false; delete child.dataset['permissionInert']; }
    }
  }
  const controls: Record<string, string[]> = {
    btnCreateAccount: ['auth.create.accounts'], btnCreateToken: ['auth.create.tokens'],
    btnLoadUsers: ['user.read'], btnShowAdd: ['user.create'], btnDoAdd: ['user.create'], btnDoDelete: ['user.delete'], btnDoReset: ['user.reset'], btnPasswordReset: ['user.password_reset'],
    btnEditDetail: Object.values(USER_FIELD_PERMISSIONS), btnLoadCodes: ['code.read'], btnShowAddCode: ['code.add.bonus', 'code.add.register'], btnDoAddCode: ['code.add.bonus', 'code.add.register'],
    btnRefreshLogs: ['audit.read'], btnPanelStatus: ['state.read.panels'], btnSystemStatus: ['state.read.host'], btnLoadSnapshots: ['snapshots.read'], btnLoadOperations: ['health.read.recovery'],
    btnRunHealth: ['health.read.status'], btnLoadOnline: ['health.read.online_users'], onlineKeyed: ['health.read.online_users'], btnDoRefresh: ['health.sync'], btnTeapot: ['health.read.teapot'],
  };
  Object.entries(controls).forEach(([id, permissions]) => { const target = doc.getElementById(id); if (target) restrict(target, permissions); });
  for (const [id, prefix] of [['lbType', 'leaderboard.read.'], ['addAction', 'code.add.']] as const) {
    const select = doc.getElementById(id);
    if (select instanceof HTMLSelectElement) { for (const option of select.options) restrict(option, [prefix + option.value]); if (select.selectedOptions[0]?.disabled) select.value = [...select.options].find(option => !option.disabled)?.value ?? ''; }
  }
  doc.querySelectorAll<HTMLElement>('.config-tab[data-section]').forEach(tab => { restrict(tab, ['config.read.' + (tab.dataset['section'] ?? '')]); });
  doc.querySelectorAll<HTMLElement>('#chartsMode [data-mode]').forEach(tab => { restrict(tab, [tab.dataset['mode'] === 'live' ? 'state.charts.read' : 'snapshots.read']); });
  for (const [id, permission] of [
    ['operationsStatusWrap', 'health.read.recovery'], ['healthText', 'health.read.status'],
    ['onlineUsersWrap', 'health.read.online_users'], ['teapotStatus', 'health.read.teapot'],
  ] as const) {
    const target = doc.getElementById(id);
    if (!target) continue;
    if (!can(permission)) target.textContent = t('permission_missing').replace('{permission}', permission);
    else if (id === 'onlineUsersWrap' && !state.panelsConfigured) target.textContent = t('no_panels');
  }
}

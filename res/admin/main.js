import { state } from './state.js';
import {
  loadUsers, bindUsersTable, hideUserDetail, doResetUser, doDeleteUser,
  showAddUser, hideAddUser, doAddUser,
} from './users.js';
import {
  loadCodes, toggleUsesRow, showAddCode, hideAddCode, doAddCode, bindCodeList,
} from './codes.js';
import { loadLogs, refreshLogs } from './logs.js';
import { checkPanelStatus, togglePanelCard } from './panels.js';
import { loadSystemStatus, loadSnapshots } from './system.js';
import {
  runHealthCheck, loadOnlineUsers, doRefreshUsers, loadOperationsStatus,
  bindOperations, checkTeapot,
} from './health.js';
import { fetchLeaderboard } from './leaderboard.js';

function renderLang() {
  applyStaticLang();
}

async function bootstrap() {
  const tokenUrl = location.pathname.replace(/\/+$/, '') + '/token';
  try {
    const res = await fetch(tokenUrl);
    if (res.status === 401) {
      state.authBlocked = true;
      toast(t('reload_reauth'), 'error');
      return;
    }
    const data = await res.json();
    if (!res.ok || !data.success || !data.obj || !data.obj.token || !data.obj.api_root) {
      state.authBlocked = true;
      toast(t('reload_reauth'), 'error');
      return;
    }
    state.apiToken = data.obj.token;
    state.baseUrl = location.origin + data.obj.api_root;
  } catch (e) {
    state.authBlocked = true;
    toast(t('network_err'), 'error');
  }
}

window.apiConfig = {
  baseUrl: () => state.baseUrl,
  headers: () => ({ 'Authorization': state.apiToken }),
  guard: () => state.authBlocked || !state.baseUrl || !state.apiToken,
  guardMessage: () => t('reload_reauth'),
  on401: () => { state.authBlocked = true; toast(t('reload_reauth'), 'error'); },
  networkError: 'toast',
  networkMessage: () => t('network_err'),
};

function showTab(name) {
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector(`.tab[onclick*="${name}"]`)?.classList.add('active');
  document.getElementById('tab-' + name)?.classList.add('active');
}

Object.assign(window, {
  showTab,
  loadUsers,
  hideUserDetail,
  doResetUser,
  doDeleteUser,
  showAddUser,
  hideAddUser,
  doAddUser,
  loadCodes,
  toggleUsesRow,
  showAddCode,
  hideAddCode,
  doAddCode,
  loadLogs,
  refreshLogs,
  checkPanelStatus,
  togglePanelCard,
  loadSystemStatus,
  loadSnapshots,
  runHealthCheck,
  loadOnlineUsers,
  doRefreshUsers,
  loadOperationsStatus,
  checkTeapot,
  fetchLeaderboard,
});

applyTheme();
initLang();
renderLang();
bootstrap();
bindUsersTable();
bindCodeList();
bindOperations();

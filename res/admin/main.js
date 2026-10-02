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
import { chartsOnTab, chartsRefreshTheme, chartsStop } from './charts.js';



function bootActions() {
  return document.querySelectorAll('.topbar .btn-sm:not(#btnLogout)');
}

function finishBoot() {
  document.getElementById('bootOverlay').hidden = true;
  document.querySelector('.admin-content').inert = false;
  bootActions().forEach(btn => { btn.disabled = false; });
}

function failBoot(key) {
  state.authBlocked = true;
  document.getElementById('bootOverlay').textContent = t(key);
}

function adminPath(suffix) {
  return location.pathname.replace(/\/+$/, '') + suffix;
}

async function bootstrap() {
  chartsStop();
  const tokenUrl = adminPath('/token');
  const loginUrl = adminPath('/login');
  try {
    const res = await fetch(tokenUrl, { credentials: 'same-origin' });
    if (res.status === 401) {
      window.location.href = loginUrl;
      return;
    }
    const data = await res.json();
    if (!res.ok || !data.success || !data.obj || !data.obj.token || !data.obj.api_root) {
      failBoot('reload_reauth');
      return;
    }
    state.apiToken = data.obj.token;
    state.baseUrl = location.origin + data.obj.api_root;
    finishBoot();
  } catch (e) {
    failBoot('network_err');
  }
}

window.apiConfig = {
  baseUrl: () => state.baseUrl,
  headers: () => ({ 'Authorization': state.apiToken }),
  guard: () => state.authBlocked || !state.baseUrl || !state.apiToken,
  guardMessage: () => t('reload_reauth'),
  on401: () => {
    state.authBlocked = true;
    window.location.href = adminPath('/login');
  },
  networkError: 'toast',
  networkMessage: () => t('network_err'),
};

async function doLogout() {
  try {
    await fetch(adminPath('/logout'), { method: 'POST', credentials: 'same-origin' });
  } catch {}
  window.location.href = adminPath('/login');
}

function showTab(name) {
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector(`.tab[onclick*="${name}"]`)?.classList.add('active');
  document.getElementById('tab-' + name)?.classList.add('active');
  chartsOnTab(name);
}

Object.assign(window, {
  showTab,
  doLogout,
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

const _applyTheme = applyTheme;
applyTheme = function () {
  _applyTheme();
  chartsRefreshTheme();
};
applyTheme();
initLang();
document.getElementById('bootOverlay').textContent = t('loading');
document.getElementById('healthText').textContent = t('not_checked');
document.getElementById('teapotStatus').textContent = t('teapot_question');
bootstrap();
bindUsersTable();
bindCodeList();
bindOperations();

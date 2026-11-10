import { createApi } from '../shared/api';
import { readBootstrap } from '../shared/bootstrap';
import { createDialogs } from '../shared/dialogs';
import { createI18n } from '../shared/i18n';
import { createNotifications } from '../shared/notifications';
import { togglePass } from '../shared/passwords';
import { createLoadGeneration } from '../shared/stale';
import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';
import { createAdminBootstrap, createAdminState } from './bootstrap';
import { initCodes } from './codes';
import { createCharts } from './charts/controller';
import { initConfigControls } from './config/controls';
import { configDispose, configOnTab, initConfig, reloadConfig, saveConfig, setConfigLanguage, showConfigSection } from './config/controller';
import { initJsonEditors } from './config/editor-bridge';
import { initConfigDialog } from './config/dialog';
import { initConfigValidation } from './config/validation';
import { button, element } from './context';
import type { AdminContext } from './context';
import { initHealth } from './health';
import { initLeaderboard } from './leaderboard';
import { initLogs } from './logs';
import { createPanels } from './panels';
import { createSystem } from './system';
import { initUsers } from './users';
import { initAuth } from './auth';
import { allows, applyPermissions, missingPermission, TAB_PERMISSIONS } from './permissions';

export function initAdmin(): () => void {
  const doc = document;
  const bootstrap = readBootstrap(doc);
  const local = safeStorage(() => localStorage);
  const session = safeStorage(() => sessionStorage);
  const i18n = createI18n(bootstrap, doc, local, session, location);
  i18n.initLang();
  const theme = createTheme(doc, local);
  const notifications = createNotifications(doc, i18n.t, (callback, delay) => window.setTimeout(callback, delay), handle => { window.clearTimeout(handle); });
  const dialogs = createDialogs(doc, i18n.t);
  const state = createAdminState();
  const auth = createAdminBootstrap(state, { doc, location, request: fetch, translate: i18n.t, logoutWarning: notifications.logoutWarning, stopCharts: () => { charts.chartsStop(); } });
  const transport = createApi(() => auth.config, i18n.t, notifications.toast, fetch);
  const can = (permission: string): boolean => allows(state.permissions, permission);
  const api: AdminContext['api'] = async (method, path, body) => {
    const missing = missingPermission(state, path, body);
    if (missing) return { success: false, status: 403, msg: i18n.t('permission_missing').replace('{permission}', missing), obj: { required_permission: missing } };
    return transport(method, path, body);
  };
  const context: AdminContext = { doc, t: i18n.t, api, can, toast: notifications.toast, confirm: dialogs.confirmDialog, loadGen: createLoadGeneration() };
  const charts = createCharts(context, {
    baseUrl: () => state.baseUrl,
    headers: () => ({ Authorization: state.apiToken }),
    guard: () => state.authBlocked || !state.baseUrl || !state.apiToken || !can('state.charts.read'),
    on401: () => auth.config.on401?.(),
  }, bootstrap.page.chartUrl, bootstrap.page.debounceMs, i18n.language);
  initConfig(context, i18n.language);
  const stopLanguage = i18n.onChange(() => { setConfigLanguage(i18n.language); charts.chartsRefreshTheme(); });
  const listeners = new AbortController();
  const cleanupControls = initConfigControls(i18n.t);
  const cleanupValidation = initConfigValidation(i18n.t, can);
  const cleanupDialog = initConfigDialog(i18n.t);
  const editorUrl = new URL('../vendor/codemirror-5.65.16.min.js', import.meta.url);
  editorUrl.search = new URL(import.meta.url).search;
  const cleanupEditors = initJsonEditors(doc, editorUrl.href, i18n.t);
  const cleanupCharts = charts.initCharts();
  const panels = createPanels(context);
  const system = createSystem(context, panels.renderPanelCards, i18n.language);
  const cleanups = [initAuth(context, state), initUsers(context, state), initCodes(context, navigator.clipboard), initLogs(context), initLeaderboard(context), initHealth(context, i18n.language), panels.init(), system.init(), cleanupCharts];
  let disposed = false;
  let leaving = false;
  let ready = false;
  let requestedTab: string | null = null;
  function loadTab(name: string): void {
    if (!ready) return;
    const refreshButtons: Record<string, string[]> = {
      auth: ['btnLoadAuth'], users: ['btnLoadUsers'], codes: ['btnLoadCodes'], logs: ['btnRefreshLogs'],
      snapshots: ['btnLoadSnapshots'], health: ['btnLoadOperations', 'btnRunHealth', 'btnLoadOnline'],
    };
    for (const id of refreshButtons[name] ?? []) {
      const refresh = button(doc, id);
      if (!refresh.disabled) refresh.click();
    }
  }
  async function showTab(name: string): Promise<void> {
    if (!ready) { requestedTab = name; return; }
    if (leaving || !doc.getElementById('tab-' + name) || !(TAB_PERMISSIONS[name] ?? ['*']).some(can)) return;
    const current = doc.querySelector<HTMLElement>('.tab-content.active');
    if (current?.id === 'tab-' + name) { if (name === 'config') configOnTab(); return; }
    leaving = true;
    try {
      if (current?.id === 'tab-config' && !await configOnTab.leave()) return;
      if (disposed) return;
      doc.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.getAttribute('data-tab') === name));
      doc.querySelectorAll('.tab-content').forEach(el => el.classList.toggle('active', el.id === 'tab-' + name));
      charts.chartsOnTab(name);
      if (name === 'config') configOnTab();
      loadTab(name);
    } finally { leaving = false; }
  }
  doc.querySelectorAll<HTMLElement>('[data-tab]').forEach(tab => { tab.addEventListener('click', () => {
    if (tab.dataset['tab']) void showTab(tab.dataset['tab']);
  }, { signal: listeners.signal }); });
  doc.querySelectorAll<HTMLElement>('.lang-btn[data-lang]').forEach(tab => { tab.addEventListener('click', () => {
    if (tab.dataset['lang']) i18n.setLang(tab.dataset['lang']);
  }, { signal: listeners.signal }); });
  button(doc, 'themeBtn').addEventListener('click', theme.toggleTheme, { signal: listeners.signal });
  button(doc, 'btnLogout').addEventListener('click', () => { void auth.logout(); }, { signal: listeners.signal });
  doc.querySelectorAll<HTMLElement>('.config-tab[data-section]').forEach(tab => { tab.addEventListener('click', () => {
    if (tab.dataset['section']) showConfigSection(tab.dataset['section']);
  }, { signal: listeners.signal }); });
  button(doc, 'configSave').addEventListener('click', () => { void saveConfig(); }, { signal: listeners.signal });
  button(doc, 'configReload').addEventListener('click', () => { void reloadConfig(); }, { signal: listeners.signal });
  for (const root of [element(doc, 'tab-config'), element(doc, 'configEditorOverlay')]) {
    root.addEventListener('click', event => {
      if (!(event.target instanceof Element)) return;
      const trigger = event.target.closest<HTMLButtonElement>('button[data-config-pass]');
      if (trigger?.dataset['configPass'] && root.contains(trigger)) togglePass(doc, trigger.dataset['configPass'], trigger);
    }, { signal: listeners.signal });
  }
  const stopTheme = theme.onChange(charts.chartsRefreshTheme);
  theme.applyTheme();
  element(doc, 'bootOverlay').textContent = i18n.t('loading');
  element(doc, 'healthText').textContent = i18n.t('not_checked');
  element(doc, 'teapotStatus').textContent = i18n.t('teapot_question');
  void auth.bootstrap().then(() => {
    if (disposed || state.authBlocked || !state.apiToken) return;
    ready = true;
    applyPermissions(doc, state, i18n.t);
    const initial = requestedTab && TAB_PERMISSIONS[requestedTab]?.some(can) ? requestedTab : Object.keys(TAB_PERMISSIONS).find(name => TAB_PERMISSIONS[name]?.some(can));
    const active = doc.querySelector<HTMLElement>('.tab-content.active');
    if (initial && active?.id !== 'tab-' + initial) void showTab(initial);
    else if (initial) loadTab(initial);
    else {
      active?.classList.remove('active');
      const notice = doc.createElement('p'); notice.className = 'result-box'; notice.textContent = i18n.t('auth_no_permissions'); doc.querySelector('.admin-content')?.append(notice);
    }
  });
  function dispose(): void {
    if (disposed) return;
    disposed = true;
    listeners.abort();
    stopLanguage();
    auth.dispose();
    cleanups.forEach(cleanup => { cleanup(); });
    stopTheme();
    configDispose();
    cleanupEditors();
    cleanupControls();
    cleanupValidation();
    cleanupDialog();
    dialogs.dispose();
    notifications.dispose();
  }
  window.addEventListener('pagehide', event => { if (!event.persisted) dispose(); }, { signal: listeners.signal });
  return dispose;
}

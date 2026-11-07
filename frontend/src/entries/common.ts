// Temporary adapter for pages whose classic scripts/inline handlers migrate in
// part 6. Shared implementation lives exclusively in importable typed modules.
import { createApi } from '../shared/api';
import type { ApiConfig } from '../shared/api';
import { readBootstrap } from '../shared/bootstrap';
import { busyDepth, withBusy } from '../shared/busy';
import { createDialogs } from '../shared/dialogs';
import { escapeHtml, loadingHtml } from '../shared/dom';
import { fmtBytes, fmtTime, fmtUptime } from '../shared/formatting';
import { createI18n } from '../shared/i18n';
import { createNotifications } from '../shared/notifications';
import { initPassToggles, togglePass } from '../shared/passwords';
import { createLoadGeneration } from '../shared/stale';
import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';

const bootstrap = readBootstrap(document);
const local = safeStorage(() => localStorage);
const session = safeStorage(() => sessionStorage);
const theme = createTheme(document, local);
// This entry is a blocking head script, before styles or visible content.
theme.applyTheme();
const i18n = createI18n(bootstrap, document, local, session, location);
const notifications = createNotifications(document, i18n.t, setTimeout);
const dialogs = createDialogs(document, i18n.t);
let apiConfig: ApiConfig = {};
const api = createApi(() => apiConfig, i18n.t, notifications.toast, fetch);

// Keep legacy consumers on the same typed API state, including SSE config.
Object.defineProperty(window, 'apiConfig', {
  configurable: true,
  get: () => apiConfig,
  set: (value: ApiConfig) => { apiConfig = value; },
});
Object.assign(window, {
  BASE: bootstrap.prefix,
  API: bootstrap.prefix + '/webapi',
  AUTH_PAGE: bootstrap.prefix + '/auth',
  DASHBOARD_PAGE: bootstrap.prefix + '/panel',
  CHART_JS: bootstrap.page.chartUrl,
  DEBOUNCE_MS: bootstrap.page.debounceMs,
  lang: i18n.language,
  t: i18n.t,
  initLang: i18n.initLang,
  setLang: i18n.setLang,
  applyStaticLang: i18n.applyStaticLang,
  applyTheme: theme.applyTheme,
  toggleTheme: theme.toggleTheme,
  onThemeChange: theme.onChange,
  api,
  busyDepth,
  withBusy,
  loadGen: createLoadGeneration(),
  escapeHtml,
  loadingHtml: () => loadingHtml(i18n.t),
  fmtBytes: (bytes: number) => fmtBytes(bytes, i18n.t),
  fmtUptime: (seconds: number | null | undefined) => fmtUptime(seconds, i18n.t),
  fmtTime,
  ...notifications,
  confirmDialog: dialogs.confirmDialog,
  togglePass: (id: string, button: HTMLElement) => togglePass(document, id, button),
  initPassToggles: () => initPassToggles(document),
});

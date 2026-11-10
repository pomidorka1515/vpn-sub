import { sharedMessage } from '../shared/messages';
import { withBusy } from '../shared/busy';
import { isRecord } from '../shared/json';
import type { ApiConfig } from '../shared/api';
import type { Translate } from '../shared/formatting';
import type { PermissionEntry } from './permissions';

export interface AdminState {
  baseUrl: string;
  apiToken: string;
  authBlocked: boolean;
  panelsConfigured: boolean;
  currentUsers: string[];
  selectedUser: string | null;
  permissions: string[];
  catalog: PermissionEntry[];
}

export function createAdminState(): AdminState {
  return { baseUrl: '', apiToken: '', authBlocked: false, panelsConfigured: true, currentUsers: [], selectedUser: null, permissions: [], catalog: [] };
}

interface Dependencies {
  doc: Document;
  location: Pick<Location, 'pathname' | 'origin' | 'href'>;
  request: typeof fetch;
  translate: Translate;
  logoutWarning: () => void;
  stopCharts: () => void;
}

// No DOM or network work occurs until bootstrap/logout is called.
export function createAdminBootstrap(state: AdminState, deps: Dependencies) {
  const { doc, location, request, translate: t } = deps;
  let disposed = false;
  const blocked = (): boolean => disposed || state.authBlocked;
  const path = (suffix: string): string => location.pathname.replace(/\/+$/, '') + suffix;
  const redirect = (): void => { if (!disposed) location.href = path('/login'); };
  const config: ApiConfig = {
    baseUrl: () => state.baseUrl,
    headers: () => ({ Authorization: state.apiToken }),
    guard: () => state.authBlocked || !state.baseUrl || !state.apiToken,
    guardMessage: () => t('reload_reauth'),
    on401: () => { state.authBlocked = true; redirect(); },
    networkError: 'toast',
    networkMessage: () => t('network_err'),
  };
  function fail(key: string): void {
    state.authBlocked = true;
    const overlay = doc.getElementById('bootOverlay');
    if (overlay) overlay.textContent = t(key);
  }
  async function bootstrap(): Promise<void> {
    deps.stopCharts();
    try {
      const response = await request(path('/token'), { credentials: 'same-origin' });
      if (blocked()) return;
      if (response.status === 401) { redirect(); return; }
      const data: unknown = await response.json();
      if (blocked()) return;
      if (!response.ok || !isRecord(data) || data['success'] !== true || !isRecord(data['obj']) ||
          typeof data['obj']['token'] !== 'string' || !data['obj']['token'] ||
          typeof data['obj']['api_root'] !== 'string' || !data['obj']['api_root']) {
        fail('reload_reauth'); return;
      }
      state.apiToken = data['obj']['token'];
      state.baseUrl = location.origin + data['obj']['api_root'];
      state.panelsConfigured = data['obj']['panels_configured'] !== false;
      state.permissions = Array.isArray(data['obj']['permissions']) ? data['obj']['permissions'].filter((value: unknown): value is string => typeof value === 'string') : [];
      state.catalog = Array.isArray(data['obj']['catalog']) ? data['obj']['catalog'].flatMap((entry: unknown): PermissionEntry[] => isRecord(entry) && typeof entry['permission'] === 'string' && (entry['requires'] === null || typeof entry['requires'] === 'string') ? [{ permission: entry['permission'], requires: entry['requires'] }] : []) : [];
      const overlay = doc.getElementById('bootOverlay');
      if (overlay) overlay.hidden = true;
      const content = doc.querySelector<HTMLElement>('.admin-content');
      if (content) content.inert = false;
      doc.querySelectorAll<HTMLButtonElement>('.topbar .btn-sm:not(#btnLogout)').forEach(button => { button.disabled = false; });
      if (!state.panelsConfigured) {
        const notice = doc.createElement('div');
        notice.className = 'result-box';
        notice.textContent = t('no_panels');
        content?.prepend(notice);
        doc.querySelectorAll<HTMLButtonElement | HTMLInputElement>('#btnShowAdd, #btnDoReset, #btnDoDelete, #btnDoAdd, #btnDoRefresh, #btnLoadOnline, #onlineKeyed').forEach(button => {
          button.disabled = true;
          button.title = t('no_panels');
        });
      }
    } catch {
      if (!disposed) fail('network_err');
    }
  }
  async function logout(): Promise<void> {
    const button = doc.querySelector<HTMLButtonElement>('#btnLogout');
    await withBusy(button, async () => {
      try {
        let token = state.apiToken;
        if (!token) {
          const response = await request(path('/token'), { credentials: 'same-origin' });
          const data: unknown = await response.json();
          if (!response.ok || !isRecord(data) || data['success'] !== true || !isRecord(data['obj']) || typeof data['obj']['token'] !== 'string' || !data['obj']['token']) throw new Error(sharedMessage('token_unavailable'));
          token = data['obj']['token'];
        }
        const response = await request(path('/logout'), { method: 'POST', credentials: 'same-origin', headers: { 'X-Admin-CSRF': token } });
        const data: unknown = await response.json().catch(() => null);
        if (!response.ok || !isRecord(data) || data['success'] !== true) throw new Error(sharedMessage('logout_unconfirmed'));
      } catch {
        try {
          const probe = await request(path('/token'), { credentials: 'same-origin', cache: 'no-store' });
          if (probe.status !== 401) { if (!disposed) deps.logoutWarning(); return; }
        } catch { if (!disposed) deps.logoutWarning(); return; }
      }
      state.apiToken = '';
      state.baseUrl = '';
      state.authBlocked = true;
      redirect();
    });
  }
  return { config, bootstrap, logout, dispose: (): void => { disposed = true; } };
}

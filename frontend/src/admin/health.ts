import { withBusy } from '../shared/busy';
import { escapeHtml, loadingHtml } from '../shared/dom';
import { fmtUptime } from '../shared/formatting';
import { isRecord } from '../shared/json';
import { renderIfChanged } from '../shared/render';
import { button, checkbox, element } from './context';
import type { AdminContext } from './context';

export function initHealth({ doc, t, api, toast, confirm, loadGen, can }: AdminContext, language: string): () => void {
  const listeners = new AbortController();
  let disposed = false;
  let healthStatus = '';
  const date = (value: unknown): string => typeof value === 'number' && Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString((doc.documentElement.lang || language) === 'ru' ? 'ru-RU' : 'en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' ' + t('utc') : t('na');
  const row = (label: string, value: unknown, cls = ''): string => `<div class="panel-metric"><span class="panel-metric-label">${label}</span><span class="panel-metric-value${cls ? ' ' + cls : ''}">${escapeHtml(value)}</span></div>`;
  async function health(): Promise<void> {
    await withBusy(button(doc, 'btnRunHealth'), async () => {
      const res = await api('GET', '/api/health');
      if (disposed) return;
      const obj = isRecord(res?.obj) ? res.obj : null;
      const status = JSON.stringify([res?.success, obj?.['degraded'], res?.msg]);
      const dot = element(doc, 'healthDot');
      const text = element(doc, 'healthText');
      if (status !== healthStatus) {
        healthStatus = status;
        if (res?.success && !obj?.['degraded']) { dot.className = 'health-dot ok'; text.textContent = t('health_ok'); text.style.color = ''; }
        else if (res?.success) { dot.className = 'health-dot warn'; text.textContent = t('health_warn'); text.style.color = 'var(--orange)'; }
        else { dot.className = 'health-dot err'; text.textContent = res?.msg || t('health_err'); text.style.color = 'var(--red)'; }
      }
      if (!res?.success || !obj) { element(doc, 'healthDetails').style.display = 'none'; return; }
      const memory = isRecord(obj['memory']) ? obj['memory'] : {};
      const ram = typeof memory['ram'] === 'number' && Number.isFinite(memory['ram']) ? memory['ram'].toFixed(2) + ' ' + t('mb_unit') : t('na');
      const swap = typeof memory['swap'] === 'number' && Number.isFinite(memory['swap']) ? memory['swap'].toFixed(2) + ' ' + t('mb_unit') : t('na');
      const details = element(doc, 'healthDetails');
      if (details.style.display) details.style.display = '';
      renderIfChanged(details, `<div class="panel-grid" style="padding-top:0"><div class="panel-section">${row(t('db'), obj['db'] ? t('yes') : t('no'), obj['db'] ? 'ok' : 'err')}${row(t('degraded'), obj['degraded'] ? t('yes') : t('no'), obj['degraded'] ? 'warn' : 'ok')}${row(t('app_uptime'), fmtUptime(typeof obj['uptime'] === 'number' ? obj['uptime'] : undefined, t))}${row(t('app_mem'), ram)}${row(t('app_swap'), swap)}${row(t('app_threads'), obj['threads'] ?? t('na'))}</div></div>`);
    });
  }
  async function online(): Promise<void> {
    if (can && !can('health.read.online_users') || checkbox(doc, 'onlineKeyed').disabled) return;
    const wrap = element(doc, 'onlineUsersWrap');
    const current = loadGen('online');
    await withBusy(button(doc, 'btnLoadOnline'), async () => {
      const keyed = checkbox(doc, 'onlineKeyed').checked;
      const res = await api('GET', keyed ? '/api/user/onlines?keyed=1' : '/api/user/onlines');
      if (disposed || !current()) return;
      if (!res?.success || !isRecord(res.obj)) { renderIfChanged(wrap, `<div class="table-empty">${escapeHtml(res?.msg || t('error'))}</div>`); return; }
      const health = isRecord(res.obj['panel_health']) ? res.obj['panel_health'] : {};
      const unhealthy = Object.entries(health).filter(([, status]) => status !== 'ok').map(([name, status]) => `${name}: ${String(status)}`);
      let html = unhealthy.length ? `<div class="table-empty">${t('partial_data').replace('{detail}', unhealthy.map(escapeHtml).join(', ') )}</div>` : '';
      const users = res.obj['users'];
      if (keyed) {
        const entries = isRecord(users) ? Object.entries(users) : [];
        if (!entries.length) html += `<div class="table-empty">${t('no_online')}</div>`;
        else html += `<table class="data-table"><thead><tr><th>${t('internal_user')}</th><th style="width:48px;text-align:center"></th><th>${t('ext_username')}</th></tr></thead><tbody>${entries.map(([internal, external]) => `<tr><td class="mono">${escapeHtml(internal)}</td><td style="text-align:center;color:var(--text2);font-size:11px"></td><td class="mono">${escapeHtml(external ?? t('na'))}</td></tr>`).join('')}</tbody></table>`;
      } else {
        const entries: unknown[] = Array.isArray(users) ? users : [];
        if (!entries.length) html += `<div class="table-empty">${t('no_online')}</div>`;
        else html += `<table class="data-table"><thead><tr><th>${t('internal_user')}</th></tr></thead><tbody>${entries.map(user => `<tr><td class="mono">${escapeHtml(user)}</td></tr>`).join('')}</tbody></table>`;
      }
      renderIfChanged(wrap, html);
    });
  }
  async function refreshUsers(): Promise<void> {
    const result = element(doc, 'refreshResult');
    const current = loadGen('refreshUsers');
    await withBusy(button(doc, 'btnDoRefresh'), async () => {
      result.innerHTML = loadingHtml(t); result.className = 'result-box';
      const res = await api('POST', '/api/user/refresh');
      if (disposed || !current()) return;
      result.innerHTML = `<div class="result-title ${res?.success ? 'ok' : 'err'}">${t(res?.success ? 'refreshed' : 'error')}</div><div class="result-msg">${escapeHtml(res?.msg || (res?.success ? '' : t('error')))}</div>`;
      result.className = `result-box ${res?.success ? 'success' : 'error'}`;
    });
  }
  function renderOperations(value: unknown): string {
    const obj = isRecord(value) ? value : {};
    const snapshot = isRecord(obj['daily_snapshot_failure']) ? obj['daily_snapshot_failure'] : null;
    const markers = isRecord(obj['rollback_failures']) ? obj['rollback_failures'] : {};
    const pending = isRecord(obj['pending_recovery']) ? obj['pending_recovery'] : {};
    const markerRows = Object.entries(markers).flatMap(([kind, users]) => isRecord(users) ? Object.entries(users).filter(([, marker]) => isRecord(marker)).map(([user, marker]) => ({ kind, user, marker: isRecord(marker) ? marker : {} })) : []);
    const pendingRows = Object.entries(pending).flatMap(([kind, users]) => Array.isArray(users) ? users.map((user: unknown) => ({ kind, user })) : []);
    if (!snapshot && !markerRows.length && !pendingRows.length) return `<div class="table-empty">${t('no_recovery_issues')}</div>`;
    const snapshotHtml = snapshot ? `<div class="panel-section-label">${t('snapshot_failure')}</div><div class="panel-cards"><div class="panel-card open"><div class="panel-card-body">${row(t('timestamp'), date(snapshot['ts']))}${row(t('failed'), String(snapshot['failed']))}${row(t('cutoff'), String(snapshot['eligible']))}</div></div></div>` : '';
    const canResolve = can?.('*') ?? true;
    const markersHtml = markerRows.length ? `<div class="panel-section-label">${t('rollback_markers')}</div><table class="data-table"><thead><tr><th>${t('internal_user')}</th><th>${t('marker_type')}</th><th>${t('timestamp')}</th><th>${t('marker_reason')}</th><th style="width:90px;text-align:center">${t('actions')}</th></tr></thead><tbody>${markerRows.map(({ kind, user, marker }) => `<tr><td class="mono">${escapeHtml(user)}</td><td>${escapeHtml(kind)}</td><td class="mono">${marker['ts'] ? escapeHtml(date(marker['ts'])) : t('na')}</td><td>${escapeHtml(marker['reason'] || '')}</td><td style="text-align:center"><button class="btn-tiny danger${canResolve ? '' : ' permission-action-blurred'}" data-rollback-kind="${escapeHtml(kind)}" data-rollback-user="${escapeHtml(user)}" ${canResolve ? '' : `disabled title="${escapeHtml(t('permission_missing').replace('{permission}', '*'))}"`}>${t('resolve')}</button></td></tr>`).join('')}</tbody></table>` : '';
    const pendingHtml = pendingRows.length ? `<table class="data-table"><thead><tr><th>${t('internal_user')}</th><th>${t('marker_type')}</th></tr></thead><tbody>${pendingRows.map(({ kind, user }) => `<tr><td class="mono">${escapeHtml(user)}</td><td>${escapeHtml(t('recovery_' + kind))} (${escapeHtml(t('pending'))})</td></tr>`).join('')}</tbody></table>` : '';
    return snapshotHtml + markersHtml + pendingHtml;
  }
  async function operations(): Promise<void> {
    const wrap = element(doc, 'operationsStatusWrap');
    const current = loadGen('operations');
    await withBusy(button(doc, 'btnLoadOperations'), async () => {
      const res = await api('GET', '/api/operations/status');
      if (disposed || !current()) return;
      if (!res?.success) { renderIfChanged(wrap, `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`); return; }
      renderIfChanged(wrap, renderOperations(res.obj));
    });
  }
  async function teapot(): Promise<void> {
    await withBusy(button(doc, 'btnTeapot'), async () => {
      const res = await api('GET', '/api/teapot');
      if (disposed) return;
      const status = element(doc, 'teapotStatus');
      if (!res || res.status !== 418 || !isRecord(res.obj) || typeof res.obj['teapot'] !== 'boolean') { status.textContent = res?.msg || t('network_err'); status.style.color = 'var(--red)'; return; }
      const yes = res.obj['teapot'];
      status.textContent = yes ? t('teapot_yes') + ' 🍵' : t('teapot_no');
      status.style.color = yes ? 'var(--orange)' : 'var(--green)';
    });
  }
  element(doc, 'operationsStatusWrap').addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const target = event.target.closest<HTMLButtonElement>('button[data-rollback-kind]');
    const kind = target?.dataset['rollbackKind'];
    const user = target?.dataset['rollbackUser'];
    if (!target || target.disabled || can && !can('*') || kind === undefined || user === undefined) return;
    void (async () => {
      if (!await confirm(t('confirm_resolve_rollback'), t('confirm'), { okText: t('confirm'), cancelText: t('cancel') }) || disposed) return;
      await withBusy(target, async () => {
        const res = await api('POST', '/api/operations/rollback/resolve', { kind, user });
        if (disposed) return;
        if (res?.success) { toast(t('resolved'), 'success'); await operations(); }
        else toast(res?.msg || t('network_err'), 'error');
      });
    })();
  }, { signal: listeners.signal });
  const bind = (id: string, event: string, action: () => Promise<void>): void => { element(doc, id).addEventListener(event, () => { void action(); }, { signal: listeners.signal }); };
  bind('btnRunHealth', 'click', health);
  bind('btnLoadOnline', 'click', online);
  bind('onlineKeyed', 'change', online);
  bind('btnDoRefresh', 'click', refreshUsers);
  bind('btnLoadOperations', 'click', operations);
  bind('btnTeapot', 'click', teapot);
  return () => { disposed = true; listeners.abort(); };
}

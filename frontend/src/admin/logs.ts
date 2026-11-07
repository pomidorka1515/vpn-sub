import { busyDepth, withBusy } from '../shared/busy';
import { escapeHtml, loadingHtml } from '../shared/dom';
import { isRecord } from '../shared/json';
import { button, element, input } from './context';
import type { AdminContext } from './context';

export function initLogs({ doc, t, api, loadGen }: AdminContext): () => void {
  const listeners = new AbortController();
  const load = button(doc, 'btnLoadLogs');
  const refresh = button(doc, 'btnRefreshLogs');
  const wrap = element(doc, 'logsTableWrap');
  let disposed = false;
  async function run(primary: HTMLButtonElement, other: HTMLButtonElement): Promise<void> {
    if (busyDepth(primary) || busyDepth(other)) return;
    const current = loadGen('logs');
    wrap.innerHTML = loadingHtml(t);
    const task = withBusy(primary, async () => {
      const count = input(doc, 'logCount').value || '50';
      const res = await api('GET', `/api/logs/audit?n=${encodeURIComponent(count)}`);
      if (disposed || !current()) return;
      if (!res?.success) {
        wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
        return;
      }
      const logs: unknown[] = Array.isArray(res.obj) ? res.obj : [];
      if (!logs.length) { wrap.innerHTML = `<div class="table-empty">${t('no_logs')}</div>`; return; }
      wrap.innerHTML = `<table class="data-table logs-table"><thead><tr><th>${t('timestamp')}</th><th>${t('log_action')}</th><th>${t('info')}</th></tr></thead><tbody>${logs.filter(isRecord).map(log => {
        const date = typeof log.date === 'string' && log.date ? log.date : new Date((typeof log.ts === 'number' ? log.ts : 0) * 1000).toLocaleString();
        return `<tr><td class="log-ts">${escapeHtml(date)}</td><td><span class="log-action ${escapeHtml(log.action)}">${escapeHtml(log.action)}</span></td><td class="log-info">${escapeHtml(JSON.stringify(log.info || {}))}</td></tr>`;
      }).join('')}</tbody></table>`;
    });
    await withBusy(other, () => task);
  }
  load.addEventListener('click', () => { void run(load, refresh); }, { signal: listeners.signal });
  refresh.addEventListener('click', () => { void run(refresh, load); }, { signal: listeners.signal });
  return () => { disposed = true; listeners.abort(); };
}

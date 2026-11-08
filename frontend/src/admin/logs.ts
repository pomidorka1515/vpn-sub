import { busyDepth, withBusy } from '../shared/busy';
import { escapeHtml } from '../shared/dom';
import { renderIfChanged } from '../shared/render';
import { isRecord } from '../shared/json';
import { button, element, input } from './context';
import type { AdminContext } from './context';

export function initLogs({ doc, t, api, loadGen }: AdminContext): () => void {
  const listeners = new AbortController();
  const refresh = button(doc, 'btnRefreshLogs');
  const wrap = element(doc, 'logsTableWrap');
  let disposed = false;
  async function run(): Promise<void> {
    if (busyDepth(refresh)) return;
    const current = loadGen('logs');
    await withBusy(refresh, async () => {
      const count = input(doc, 'logCount').value || '50';
      const res = await api('GET', `/api/logs/audit?n=${encodeURIComponent(count)}`);
      if (disposed || !current()) return;
      if (!res?.success) {
        renderIfChanged(wrap, `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`);
        return;
      }
      const logs: unknown[] = Array.isArray(res.obj) ? res.obj : [];
      if (!logs.length) { renderIfChanged(wrap, `<div class="table-empty">${t('no_logs')}</div>`); return; }
      renderIfChanged(wrap, `<table class="data-table logs-table"><thead><tr><th>${t('timestamp')}</th><th>${t('log_action')}</th><th>${t('info')}</th></tr></thead><tbody>${logs.filter(isRecord).map(log => {
        const date = typeof log.date === 'string' && log.date ? log.date : new Date((typeof log.ts === 'number' ? log.ts : 0) * 1000).toLocaleString();
        return `<tr><td class="log-ts">${escapeHtml(date)}</td><td><span class="log-action ${escapeHtml(log.action)}">${escapeHtml(log.action)}</span></td><td class="log-info">${escapeHtml(JSON.stringify(log.info || {}))}</td></tr>`;
      }).join('')}</tbody></table>`);
    });
  }
  refresh.addEventListener('click', () => { void run(); }, { signal: listeners.signal });
  return () => { disposed = true; listeners.abort(); };
}

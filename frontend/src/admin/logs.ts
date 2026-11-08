import { busyDepth, withBusy } from '../shared/busy';
import { escapeHtml } from '../shared/dom';
import { renderIfChanged } from '../shared/render';
import { isRecord } from '../shared/json';
import type { Translate } from '../shared/formatting';
import { button, element, input } from './context';
import type { AdminContext } from './context';

function logDate(log: Record<string, unknown>): string {
  return typeof log.date === 'string' && log.date ? log.date : new Date((typeof log.ts === 'number' ? log.ts : 0) * 1000).toLocaleString();
}

function infoValue(value: unknown, t: Translate): string {
  if (Array.isArray(value)) return value.length ? `<ol class="log-value-list">${value.map(item => `<li>${infoValue(item, t)}</li>`).join('')}</ol>` : escapeHtml(t('log_empty_info'));
  if (isRecord(value)) return Object.keys(value).length ? `<dl class="log-fields">${Object.entries(value).map(([key, item]) => `<div><dt>${escapeHtml(key.replace(/[_-]/g, ' '))}</dt><dd>${infoValue(item, t)}</dd></div>`).join('')}</dl>` : escapeHtml(t('log_empty_info'));
  if (value == null) return escapeHtml(t('na'));
  if (typeof value === 'boolean') return escapeHtml(t(value ? 'yes' : 'no'));
  return `<span class="log-value">${escapeHtml(String(value))}</span>`;
}

function infoSummary(value: unknown, t: Translate): string {
  if (isRecord(value)) return Object.entries(value).map(([key, item]) => `${key.replace(/[_-]/g, ' ')}: ${infoSummary(item, t)}`).join(' · ') || t('log_empty_info');
  if (Array.isArray(value)) return value.map(item => infoSummary(item, t)).join(', ') || t('log_empty_info');
  return value == null ? t('na') : typeof value === 'boolean' ? t(value ? 'yes' : 'no') : String(value);
}

export function initLogs({ doc, t, api, loadGen }: AdminContext): () => void {
  const listeners = new AbortController();
  const refresh = button(doc, 'btnRefreshLogs');
  const wrap = element(doc, 'logsTableWrap');
  const filter = input(doc, 'logActionFilter');
  const dialog = doc.createElement('dialog');
  dialog.className = 'modal log-dialog';
  dialog.setAttribute('aria-labelledby', 'logDetailTitle');
  doc.body.append(dialog);
  let opener: HTMLElement | null = null;
  let logs: Record<string, unknown>[] = [];
  let disposed = false;
  function render(): void {
    const visible = logs.map((log, index) => ({ log, index })).filter(({ log }) => !filter.value || log.action === filter.value);
    if (!visible.length) {
      renderIfChanged(wrap, `<div class="table-empty">${escapeHtml(t(logs.length ? 'log_no_matches' : 'no_logs'))}</div>`);
      return;
    }
    renderIfChanged(wrap, `<table class="data-table logs-table"><thead><tr><th>${t('timestamp')}</th><th>${t('log_action')}</th><th>${t('info')}</th></tr></thead><tbody>${visible.map(({ log, index }) =>
      `<tr data-log-index="${index}" tabindex="0" aria-haspopup="dialog"><td class="log-ts">${escapeHtml(logDate(log))}</td><td><span class="log-action ${escapeHtml(log.action)}">${escapeHtml(log.action)}</span></td><td class="log-info">${escapeHtml(infoSummary(log.info ?? {}, t))}</td></tr>`
    ).join('')}</tbody></table>`);
  }
  function open(row: HTMLElement): void {
    const log = logs[Number(row.dataset.logIndex)];
    if (!log) return;
    opener = row;
    dialog.innerHTML = `<h2 class="modal-title" id="logDetailTitle">${escapeHtml(t('log_details'))}</h2><dl class="log-fields log-meta"><div><dt>${escapeHtml(t('timestamp'))}</dt><dd>${escapeHtml(logDate(log))}</dd></div><div><dt>${escapeHtml(t('log_action'))}</dt><dd>${escapeHtml(log.action)}</dd></div></dl><h3>${escapeHtml(t('info'))}</h3><div class="log-detail-info">${infoValue(log.info ?? {}, t)}</div><div class="modal-actions"><button type="button" class="modal-btn" data-log-close>${escapeHtml(t('close'))}</button></div>`;
    dialog.showModal();
  }
  wrap.addEventListener('click', event => {
    const row = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-log-index]') : null;
    if (row) open(row);
  }, { signal: listeners.signal });
  wrap.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const row = event.target instanceof HTMLElement && event.target.matches('[data-log-index]') ? event.target : null;
    if (row) { event.preventDefault(); open(row); }
  }, { signal: listeners.signal });
  dialog.addEventListener('click', event => {
    if (event.target instanceof Element && event.target.closest('[data-log-close]')) dialog.close();
    else if (event.target === dialog) {
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    }
  }, { signal: listeners.signal });
  dialog.addEventListener('close', () => { if (opener?.isConnected) opener.focus(); }, { signal: listeners.signal });
  filter.addEventListener('change', render, { signal: listeners.signal });
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
      logs = Array.isArray(res.obj) ? res.obj.filter(isRecord) : [];
      const selected = filter.value;
      const actions = [...new Set(logs.map(log => log.action).filter((action): action is string => typeof action === 'string' && !!action))].sort();
      renderIfChanged(filter, `<option value="">${escapeHtml(t('log_all_actions'))}</option>${actions.map(action => `<option value="${escapeHtml(action)}">${escapeHtml(action)}</option>`).join('')}`);
      filter.value = actions.includes(selected) ? selected : '';
      render();
    });
  }
  refresh.addEventListener('click', () => { void run(); }, { signal: listeners.signal });
  return () => { disposed = true; if (dialog.open) dialog.close(); listeners.abort(); dialog.remove(); };
}

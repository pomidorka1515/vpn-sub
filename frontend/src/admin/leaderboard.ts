import { withBusy } from '../shared/busy';
import { escapeHtml, loadingHtml } from '../shared/dom';
import { fmtBytes } from '../shared/formatting';
import { isRecord } from '../shared/json';
import { button, checkbox, element, input } from './context';
import type { AdminContext } from './context';

export function initLeaderboard({ doc, t, api, loadGen }: AdminContext): () => void {
  const listeners = new AbortController();
  const fetchButton = button(doc, 'btnFetchLb');
  const wrap = element(doc, 'lbTableWrap');
  let disposed = false;
  async function fetchLeaderboard(): Promise<void> {
    const cutoff = parseInt(input(doc, 'lbCutoff').value, 10) || 0;
    const body = {
      type: input(doc, 'lbType').value,
      cutoff: Math.max(0, cutoff),
      displaynames: checkbox(doc, 'lbDisplaynames').checked,
      flip: checkbox(doc, 'lbFlip').checked,
    };
    const current = loadGen('leaderboard');
    await withBusy(fetchButton, async () => {
      wrap.innerHTML = loadingHtml(t);
      const res = await api('POST', '/api/leaderboard', body);
      if (disposed || !current()) return;
      if (!res?.success) {
        wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('error'))}</div>`;
        return;
      }
      const entries: unknown[] = Array.isArray(res.obj) ? res.obj : [];
      if (!entries.length) {
        wrap.innerHTML = `<div class="table-empty">${t('no_entries')}</div>`;
        return;
      }
      wrap.innerHTML = `<table class="data-table"><thead><tr><th style="width:50px">${t('lb_rank')}</th><th>${t('lb_user')}</th><th style="text-align:right">${t('lb_bandwidth')}</th></tr></thead><tbody>${entries
        .filter(isRecord)
        .map(
          (entry) =>
            `<tr><td class="mono" style="color:var(--text2)">${escapeHtml(entry['place'])}</td><td class="mono">${escapeHtml(entry['username'])}</td><td class="mono" style="text-align:right">${fmtBytes(typeof entry['amount'] === 'number' ? entry['amount'] : NaN, t)}</td></tr>`,
        )
        .join('')}</tbody></table>`;
    });
  }
  fetchButton.addEventListener(
    'click',
    () => {
      void fetchLeaderboard();
    },
    { signal: listeners.signal },
  );
  return () => {
    disposed = true;
    listeners.abort();
  };
}

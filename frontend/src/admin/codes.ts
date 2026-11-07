import { withBusy } from '../shared/busy';
import { escapeHtml, loadingHtml } from '../shared/dom';
import { isRecord } from '../shared/json';
import { button, checkbox, element, input } from './context';
import type { AdminContext } from './context';

export function initCodes({ doc, t, api, toast, confirm, loadGen }: AdminContext, clipboard: Pick<Clipboard, 'writeText'>): () => void {
  const listeners = new AbortController();
  const list = element(doc, 'codeList');
  let disposed = false;
  const options = (): { okText: string; cancelText: string } => ({ okText: t('confirm'), cancelText: t('cancel') });
  function toggleUsesRow(): void { element(doc, 'addUsesRow').style.display = checkbox(doc, 'addPerma').checked ? 'none' : 'flex'; }
  function hideAddCode(): void { element(doc, 'addCodeCard').style.display = 'none'; }
  function showAddCode(): void {
    element(doc, 'addCodeCard').style.display = 'block';
    for (const [id, value] of [['addCode', ''], ['addAction', 'bonus'], ['addDays', '0'], ['addGb', '0'], ['addWlGb', '0'], ['addUses', '1']] as const) input(doc, id).value = value;
    checkbox(doc, 'addPerma').checked = false;
    toggleUsesRow();
    const result = element(doc, 'addCodeResult');
    result.style.display = 'none'; result.innerHTML = '';
    input(doc, 'addCode').focus();
  }
  async function loadCodes(): Promise<void> {
    const current = loadGen('codes');
    await withBusy(button(doc, 'btnLoadCodes'), async () => {
      list.innerHTML = loadingHtml(t);
      const res = await api('GET', '/api/code/list');
      if (disposed || !current()) return;
      if (!res?.success) { list.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`; return; }
      const names = Array.isArray(res.obj) ? res.obj.filter((name: unknown): name is string => typeof name === 'string') : [];
      if (!names.length) { list.innerHTML = `<div class="table-empty">${t('no_codes')}</div>`; return; }
      const entries: Array<[string, Record<string, unknown> | null]> = [];
      for (const name of names) {
        const info = await api('GET', `/api/code/info?code=${encodeURIComponent(name)}`);
        if (disposed || !current()) return;
        entries.push([name, info?.success && isRecord(info.obj) ? info.obj : null]);
      }
      list.innerHTML = entries.map(([name, code]) => {
        let badges = '';
        const meta: string[] = [];
        if (code) {
          badges += `<span class="badge ${code.action === 'register' ? 'purple' : 'cyan'}">${escapeHtml(code.action || '—')}</span>`;
          badges += code.perma ? `<span class="badge green">${t('reusable')}</span>` : `<span class="badge gray">${escapeHtml(code.uses ?? 1)} ${t('uses').toLowerCase()}</span>`;
          if (code.days) meta.push(`<span>${escapeHtml(code.days)} ${t('days').toLowerCase()}</span>`);
          if (code.gb) meta.push(`<span>${escapeHtml(code.gb)} ${t('gb')}</span>`);
          if (code.wl_gb) meta.push(`<span>${escapeHtml(code.wl_gb)} ${t('gb')} ${t('wl')}</span>`);
        }
        return `<div class="code-card"><div class="code-info"><div class="code-name">${escapeHtml(name)}</div><div class="code-meta">${badges}${meta.join('')}</div></div><div class="code-actions"><button class="btn-tiny" data-code-action="copy" data-code="${escapeHtml(name)}">${t('copy')}</button><button class="btn-tiny danger" data-code-action="delete" data-code="${escapeHtml(name)}">${t('delete')}</button></div></div>`;
      }).join('');
    });
  }
  async function addCode(): Promise<void> {
    const code = input(doc, 'addCode').value.trim();
    if (!code) { toast(t('code_required'), 'error'); return; }
    const perma = checkbox(doc, 'addPerma').checked;
    const body: { code: string; action: string; perma: boolean; days: number; gb: number; wl_gb: number; uses?: number } = {
      code, action: input(doc, 'addAction').value, perma,
      days: parseInt(input(doc, 'addDays').value, 10) || 0,
      gb: parseInt(input(doc, 'addGb').value, 10) || 0,
      wl_gb: parseInt(input(doc, 'addWlGb').value, 10) || 0,
    };
    if (!perma) {
      const uses = parseInt(input(doc, 'addUses').value, 10);
      if (!Number.isFinite(uses) || uses <= 0) { toast(t('uses_invalid'), 'error'); return; }
      body.uses = uses;
    }
    await withBusy(button(doc, 'btnDoAddCode'), async () => {
      const res = await api('POST', '/api/code/add', body);
      if (disposed) return;
      if (res?.success) { toast(t('created'), 'success'); hideAddCode(); void loadCodes(); }
      else {
        const result = element(doc, 'addCodeResult');
        result.style.display = 'block'; result.className = 'result-box error';
        result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
      }
    });
  }
  list.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const target = event.target.closest<HTMLButtonElement>('button[data-code-action]');
    if (!target || !list.contains(target)) return;
    const code = target.dataset.code;
    if (code === undefined) return;
    if (target.dataset.codeAction === 'copy') {
      void withBusy(target, async () => {
        try { await clipboard.writeText(code); if (!disposed) toast(t('copied'), 'success'); }
        catch { if (!disposed) toast(t('copy_failed'), 'error'); }
      });
    } else if (target.dataset.codeAction === 'delete') {
      void (async () => {
        if (!await confirm(t('confirm_delete_code'), t('confirm'), options()) || disposed) return;
        await withBusy(target, async () => {
          const res = await api('POST', '/api/code/delete', { code });
          if (disposed) return;
          if (res?.success) { toast(t('deleted'), 'success'); void loadCodes(); }
          else toast(res?.msg || t('error'), 'error');
        });
      })();
    }
  }, { signal: listeners.signal });
  const bind = (id: string, event: string, handler: () => void): void => element(doc, id).addEventListener(event, handler, { signal: listeners.signal });
  bind('btnLoadCodes', 'click', () => { void loadCodes(); });
  bind('btnShowAddCode', 'click', showAddCode);
  bind('btnCancelAddCode', 'click', hideAddCode);
  bind('btnDoAddCode', 'click', () => { void addCode(); });
  bind('addPerma', 'change', toggleUsesRow);
  return () => { disposed = true; listeners.abort(); };
}

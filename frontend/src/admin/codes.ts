import { withBusy } from '../shared/busy';
import { escapeHtml } from '../shared/dom';
import { isRecord } from '../shared/json';
import { button, checkbox, element, input } from './context';
import type { AdminContext } from './context';

export function initCodes(
  { doc, t, api, toast, confirm, loadGen, can = () => true }: AdminContext,
  clipboard: Pick<Clipboard, 'writeText'>,
): () => void {
  const listeners = new AbortController();
  const list = element(doc, 'codeList');
  const entries = new Map<string, { node: HTMLElement; html: string; info: Record<string, unknown> | null }>();
  let loaded = false;
  let loading = false;
  let revision = 0;
  let disposed = false;
  const isDisposed = (): boolean => disposed;
  const options = (): { okText: string; cancelText: string } => ({ okText: t('confirm'), cancelText: t('cancel') });
  function toggleUsesRow(): void {
    element(doc, 'addUsesRow').style.display = checkbox(doc, 'addPerma').checked ? 'none' : 'flex';
  }
  function hideAddCode(): void {
    element(doc, 'addCodeCard').style.display = 'none';
    button(doc, 'btnShowAddCode').setAttribute('aria-expanded', 'false');
  }
  function showAddCode(): void {
    element(doc, 'addCodeCard').style.display = 'block';
    button(doc, 'btnShowAddCode').setAttribute('aria-expanded', 'true');
    for (const [id, value] of [
      ['addCode', ''],
      ['addDays', '0'],
      ['addGb', '0'],
      ['addWlGb', '0'],
      ['addUses', '1'],
    ] as const)
      input(doc, id).value = value;
    input(doc, 'addAction').value = ['bonus', 'register'].find((action) => can('code.add.' + action)) ?? '';
    checkbox(doc, 'addPerma').checked = false;
    toggleUsesRow();
    const result = element(doc, 'addCodeResult');
    result.style.display = 'none';
    result.innerHTML = '';
    input(doc, 'addCode').focus();
  }
  function updateEmptyState(): void {
    if (!entries.size && loaded && !list.querySelector('[data-codes-empty]')) {
      list.innerHTML = `<div class="code-empty" data-codes-empty><strong>${escapeHtml(t('no_codes'))}</strong><p>${escapeHtml(t('codes_empty_hint'))}</p></div>`;
    }
  }
  function codeHtml(name: string, code: Record<string, unknown> | null): string {
    const action = code?.['action'] === 'register' ? 'register' : 'bonus';
    const badges = code
      ? `<span class="code-type ${action}">${escapeHtml(t(action))}</span><span class="code-usage">${code['perma'] ? escapeHtml(t('reusable')) : `${escapeHtml(code['uses'] ?? 1)} ${escapeHtml(t('uses').toLowerCase())}`}</span>`
      : '';
    const metrics = code
      ? [
          ['days', code['days']],
          ['gb', code['gb']],
          ['wl_gb', code['wl_gb']],
        ]
          .map(
            ([key, value]) =>
              `<div><dt>${escapeHtml(t(String(key)))}</dt><dd>${escapeHtml(action === 'register' && value === 0 ? t('codes_unlimited_value') : (value ?? 0))}</dd></div>`,
          )
          .join('')
      : '';
    const deleteAccess = can('code.delete')
      ? ''
      : `disabled title="${escapeHtml(t('permission_missing').replace('{permission}', 'code.delete'))}"`;
    return `<div class="code-info"><div class="code-heading"><h3 class="code-name">${escapeHtml(name)}</h3><div class="code-badges">${badges}</div></div>${code ? `<dl class="code-metrics">${metrics}</dl>` : `<p class="code-unavailable">${escapeHtml(t('codes_info_unavailable'))}</p>`}</div><div class="code-actions"><button class="btn-tiny" data-code-action="copy" data-code="${escapeHtml(name)}">${escapeHtml(t('copy'))}</button><button class="btn-tiny danger" data-code-action="delete" data-code="${escapeHtml(name)}" ${deleteAccess}>${escapeHtml(t('delete'))}</button></div>`;
  }
  function reconcile(next: Array<[string, Record<string, unknown> | null]>): void {
    const names = new Set(next.map(([name]) => name));
    for (const [name, entry] of entries) {
      if (!names.has(name)) {
        entry.node.remove();
        entries.delete(name);
      }
    }
    if (next.length && !entries.size) list.replaceChildren();
    let previous: HTMLElement | null = null;
    for (const [name, info] of next) {
      const html = codeHtml(name, info);
      let entry = entries.get(name);
      if (!entry) {
        const node = doc.createElement('article');
        node.className = 'code-card';
        node.innerHTML = html;
        entry = { node, html, info };
        entries.set(name, entry);
      } else if (entry.html !== html) {
        // Keep the action buttons (and keyboard focus) when only details change.
        const template = doc.createElement('template');
        template.innerHTML = html;
        const replacement = template.content.firstElementChild;
        if (replacement) entry.node.querySelector('.code-info')?.replaceWith(replacement);
        entry.html = html;
        entry.info = info;
      }
      const expected: Element | null = previous ? previous.nextElementSibling : list.firstElementChild;
      if (expected !== entry.node) list.insertBefore(entry.node, expected);
      previous = entry.node;
    }
    loaded = true;
    updateEmptyState();
  }
  async function loadCodes(): Promise<void> {
    if (loading || disposed) return;
    loading = true;
    const current = loadGen('codes');
    const started = revision;
    try {
      await withBusy(button(doc, 'btnLoadCodes'), async () => {
        const res = await api('GET', '/api/code/list');
        if (disposed || !current() || started !== revision) return;
        if (!res?.success || !Array.isArray(res.obj)) {
          const message = res?.msg || t('network_err');
          if (!loaded) list.textContent = message;
          toast(message, 'error');
          return;
        }
        const names = [...new Set(res.obj.filter((name: unknown): name is string => typeof name === 'string'))];
        const next: Array<[string, Record<string, unknown> | null]> = [];
        for (const name of names) {
          const info = await api('GET', `/api/code/info?code=${encodeURIComponent(name)}`);
          if (!current() || started !== revision || isDisposed()) return;
          next.push([name, info?.success && isRecord(info.obj) ? info.obj : (entries.get(name)?.info ?? null)]);
        }
        reconcile(next);
      });
    } finally {
      loading = false;
    }
  }
  async function addCode(): Promise<void> {
    if (!can('code.add.' + input(doc, 'addAction').value)) return;
    const code = input(doc, 'addCode').value.trim();
    if (!code) {
      toast(t('code_required'), 'error');
      return;
    }
    const perma = checkbox(doc, 'addPerma').checked;
    const body: {
      code: string;
      action: string;
      perma: boolean;
      days: number;
      gb: number;
      wl_gb: number;
      uses?: number;
    } = {
      code,
      action: input(doc, 'addAction').value,
      perma,
      days: parseInt(input(doc, 'addDays').value, 10) || 0,
      gb: parseInt(input(doc, 'addGb').value, 10) || 0,
      wl_gb: parseInt(input(doc, 'addWlGb').value, 10) || 0,
    };
    if (!perma) {
      const uses = parseInt(input(doc, 'addUses').value, 10);
      if (!Number.isFinite(uses) || uses <= 0) {
        toast(t('uses_invalid'), 'error');
        return;
      }
      body.uses = uses;
    }
    await withBusy(button(doc, 'btnDoAddCode'), async () => {
      const res = await api('POST', '/api/code/add', body);
      if (disposed) return;
      if (res?.success) {
        revision++;
        const next: Array<[string, Record<string, unknown> | null]> = [...entries].map(([name, entry]) => [
          name,
          entry.info,
        ]);
        reconcile([...next.filter(([name]) => name !== code), [code, body]]);
        toast(t('created'), 'success');
        hideAddCode();
      } else {
        const result = element(doc, 'addCodeResult');
        result.style.display = 'block';
        result.className = 'result-box error';
        result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
      }
    });
  }
  list.addEventListener(
    'click',
    (event) => {
      if (!(event.target instanceof Element)) return;
      const target = event.target.closest<HTMLButtonElement>('button[data-code-action]');
      if (!target || target.disabled || !list.contains(target)) return;
      const code = target.dataset['code'];
      if (code === undefined) return;
      if (target.dataset['codeAction'] === 'copy') {
        void withBusy(target, async () => {
          try {
            await clipboard.writeText(code);
            if (!disposed) toast(t('copied'), 'success');
          } catch {
            if (!disposed) toast(t('copy_failed'), 'error');
          }
        });
      } else if (target.dataset['codeAction'] === 'delete' && can('code.delete')) {
        void (async () => {
          if (!(await confirm(t('confirm_delete_code'), t('confirm'), options())) || disposed) return;
          await withBusy(target, async () => {
            const res = await api('POST', '/api/code/delete', { code });
            if (disposed) return;
            if (res?.success) {
              revision++;
              const entry = entries.get(code);
              const focused = entry?.node.contains(doc.activeElement);
              const neighbor = entry?.node.nextElementSibling ?? entry?.node.previousElementSibling;
              entry?.node.remove();
              entries.delete(code);
              updateEmptyState();
              if (focused)
                (neighbor?.querySelector<HTMLButtonElement>('button') ?? button(doc, 'btnShowAddCode')).focus();
              toast(t('deleted'), 'success');
            } else toast(res?.msg || t('error'), 'error');
          });
        })();
      }
    },
    { signal: listeners.signal },
  );
  const bind = (id: string, event: string, handler: () => void): void => {
    element(doc, id).addEventListener(event, handler, { signal: listeners.signal });
  };
  bind('btnLoadCodes', 'click', () => {
    void loadCodes();
  });
  bind('btnShowAddCode', 'click', showAddCode);
  bind('btnCancelAddCode', 'click', hideAddCode);
  bind('btnDoAddCode', 'click', () => {
    void addCode();
  });
  bind('addPerma', 'change', toggleUsesRow);
  return () => {
    disposed = true;
    listeners.abort();
  };
}

import { withBusy } from '../shared/busy';
import { escapeHtml } from '../shared/dom';
import { ICON_DELETE, ICON_EDIT, ICON_INFO } from '../shared/icons';
import { isRecord } from '../shared/json';
import type { AdminState } from './bootstrap';
import { button, checkbox, element, input } from './context';
import type { AdminContext } from './context';
import { allows, foldPermissions, updateActionPermissionNotices } from './permissions';

type Kind = 'accounts' | 'tokens';
interface Identity { id: string; kind: Kind; name: string; permissions: string[]; root: boolean }
function parseIdentity(value: unknown, kind: Kind): Identity | null {
  if (!isRecord(value) || typeof value['id'] !== 'string' || typeof value['name'] !== 'string' || value['kind'] !== kind || typeof value['root'] !== 'boolean' || !Array.isArray(value['permissions']) || !value['permissions'].every((grant: unknown) => typeof grant === 'string')) return null;
  return { id: value['id'], kind, name: value['name'], permissions: value['permissions'], root: value['root'] };
}
export function initAuth(context: AdminContext, state: AdminState): () => void {
  const { doc, t, api, toast, confirm, loadGen } = context;
  const listeners = new AbortController();
  const can = (permission: string): boolean => allows(state.permissions, permission);
  const identities = new Map<Kind, Identity[]>([['accounts', []], ['tokens', []]]);
  const errors = new Map<Kind, string>();
  const getDialog = (id: string): HTMLDialogElement => { const dialog = element(doc, id); if (!(dialog instanceof HTMLDialogElement)) throw new Error(t('error')); return dialog; };
  const dialog = getDialog('authDialog');
  const secretDialog = getDialog('authSecretDialog');
  const form = element(doc, 'authForm');
  const tree = element(doc, 'authPermissionTree');
  const error = element(doc, 'authError');
  const clearError = (): void => { error.hidden = true; error.textContent = ''; };
  let kind: Kind = 'accounts';
  let selected = new Set<string>();
  let current: Identity | null = null;
  let readonly = false;
  let disposed = false;
  let opener: HTMLElement | null = null;
  const hint = (permission: string): string => t('permission_missing').replace('{permission}', permission);
  const editable = (item: Identity): boolean => can('auth.update.' + item.kind) && item.permissions.every(can);
  function render(kind: Kind): void {
    state.identityGrants = [...identities.values()].flat().flatMap(item => can('auth.update.' + item.kind) ? item.permissions.filter(permission => !can(permission)) : []);
    updateActionPermissionNotices(doc, state, t);
    const wrap = element(doc, 'auth' + kind + 'Table');
    const query = input(doc, 'authSearch').value.trim().toLowerCase();
    const items = (identities.get(kind) ?? []).filter(item => item.name.toLowerCase().includes(query));
    if (!can('auth.read.' + kind)) { wrap.textContent = hint('auth.read.' + kind); return; }
    const error = errors.get(kind);
    if (error) { wrap.textContent = error; return; }
    if (!items.length) { wrap.innerHTML = `<div class="table-empty">${escapeHtml(t('auth_empty'))}</div>`; return; }
    wrap.innerHTML = `<table class="data-table users-table"><thead><tr><th>${escapeHtml(t('auth_name'))}</th><th>${escapeHtml(t('auth_permissions'))}</th><th>${escapeHtml(t('actions'))}</th></tr></thead><tbody>${items.map(item => `<tr><td class="mono user-name">${escapeHtml(item.name)}${item.root ? `<span class="badge">${escapeHtml(t('auth_root'))}</span>` : ''}</td><td class="mono auth-grants">${escapeHtml(item.permissions.join(', ') || t('auth_no_permissions'))}</td><td><div class="actions">${([['info', ICON_INFO], ['edit', ICON_EDIT], ['delete', ICON_DELETE]] as const).map(([action, icon]) => {
      const allowed = action === 'info' || editable(item) && (action !== 'delete' || !item.root);
      const title = allowed ? t(action) : item.root && action === 'delete' ? t('auth_root_protected') : hint(can('auth.update.' + kind) ? item.permissions.find(permission => !can(permission)) ?? 'auth.update.' + kind : 'auth.update.' + kind);
      return `<button class="btn-tiny user-action user-${action === 'info' ? 'view' : action}${action === 'delete' ? ' danger' : ''}${!allowed && !(item.root && action === 'delete') ? ' permission-action-disabled' : ''}" data-auth-kind="${kind}" data-auth-id="${escapeHtml(item.id)}" data-auth-action="${action}" title="${escapeHtml(title)}" aria-label="${escapeHtml(t(action))}" ${allowed ? '' : 'disabled'}>${icon}<span class="user-action-label">${escapeHtml(t(action))}</span></button>`;
    }).join('')}</div></td></tr>`).join('')}</tbody></table>`;
  }
  async function load(): Promise<void> {
    const generation = loadGen('admin-identities');
    await withBusy(button(doc, 'btnLoadAuth'), async () => {
      await Promise.all((['accounts', 'tokens'] as const).map(async kind => {
        if (!can('auth.read.' + kind)) { render(kind); return; }
        const response = await api('GET', '/api/auth/list?kind=' + kind);
        if (disposed || !generation()) return;
        if (!response?.success || !isRecord(response.obj) || !Array.isArray(response.obj['items'])) {
          const message = response?.msg || t('network_err');
          errors.set(kind, message); render(kind);
          toast(message, 'error'); return;
        }
        errors.delete(kind);
        identities.set(kind, response.obj['items'].flatMap((value: unknown) => { const item = parseIdentity(value, kind); return item ? [item] : []; }));
        render(kind);
      }));
    });
    if (disposed) return;
    for (const value of ['accounts', 'tokens'] as const) {
      const create = button(doc, value === 'accounts' ? 'btnCreateAccount' : 'btnCreateToken');
      create.disabled = !can('auth.create.' + value);
      create.title = create.disabled ? hint('auth.create.' + value) : '';
    }
  }
  function renderTree(): void {
    const focused = doc.activeElement instanceof HTMLInputElement && tree.contains(doc.activeElement) ? doc.activeElement.dataset['permission'] : undefined;
    tree.replaceChildren();
    const catalog = state.catalog;
    const build = (prefix: string, depth: number): void => {
      const leaves = catalog.filter(entry => !prefix || entry.permission.startsWith(prefix + '.'));
      const permission = prefix ? prefix + '.*' : '*';
      const label = doc.createElement('label'); label.className = 'permission-row'; label.style.paddingInlineStart = String(depth * 6 + 6) + 'px';
      const check = doc.createElement('input'); check.type = 'checkbox'; check.dataset['permission'] = permission;
      const amount = leaves.filter(entry => selected.has(entry.permission)).length;
      check.checked = amount === leaves.length && leaves.length > 0; check.indeterminate = amount > 0 && amount < leaves.length;
      check.disabled = readonly || !!current?.root || !can(permission);
      if (!current?.root && !can(permission)) check.title = hint(permission);
      const text = doc.createElement('span'); text.className = 'mono'; text.textContent = permission;
      label.title = t(prefix ? 'permission_branch_tip' : 'permission_root_tip').replace('{permission}', permission);
      label.append(check, text); tree.append(label);
      const children = new Set(leaves.map(entry => entry.permission.slice(prefix ? prefix.length + 1 : 0).split('.')[0] ?? ''));
      for (const child of children) {
        const name = prefix ? prefix + '.' + child : child;
        const leaf = catalog.find(entry => entry.permission === name);
        if (!leaf) { build(name, depth + 1); continue; }
        const row = doc.createElement('label'); row.className = 'permission-row'; row.style.paddingInlineStart = String((depth + 1) * 6 + 6) + 'px';
        const field = doc.createElement('input'); field.type = 'checkbox'; field.dataset['permission'] = name; field.checked = selected.has(name); field.disabled = readonly || !!current?.root || !can(name);
        if (!current?.root && !can(name)) field.title = hint(name);
        const text = doc.createElement('span'); text.className = 'mono'; text.textContent = name;
        row.title = t('permission_' + name.replaceAll('.', '_'));
        row.append(field, text); tree.append(row);
      }
    };
    build('', 0);
    element(doc, 'authPermissionSummary').textContent = JSON.stringify(current?.root ? ['*'] : readonly && current ? current.permissions : foldPermissions(selected, catalog, can));
    if (focused) [...tree.querySelectorAll<HTMLInputElement>('input')].find(field => field.dataset['permission'] === focused)?.focus();
  }
  function open(value: Kind, item: Identity | null, info: boolean, trigger: HTMLElement): void {
    if (!can('auth.read.' + value) || (!info && !(item ? editable(item) : can('auth.create.' + value)))) return;
    kind = value; current = item; readonly = info; opener = trigger;
    clearError();
    selected = new Set(state.catalog.filter(entry => allows(item?.permissions ?? [], entry.permission)).map(entry => entry.permission));
    input(doc, 'authName').value = item?.name ?? ''; input(doc, 'authName').disabled = info || item?.root === true && value === 'tokens';
    input(doc, 'authPassword').value = ''; input(doc, 'authPassword').required = !item && value === 'accounts';
    element(doc, 'authPasswordRow').hidden = info || value !== 'accounts';
    element(doc, 'authRotateRow').hidden = info || !item || value !== 'tokens'; checkbox(doc, 'authRotate').checked = false;
    element(doc, 'authDialogTitle').textContent = t(info ? 'auth_info' : item ? 'auth_edit' : value === 'accounts' ? 'auth_create_account' : 'auth_create_token');
    element(doc, 'authRootHint').hidden = !item?.root;
    button(doc, 'authSave').hidden = info;
    renderTree(); dialog.showModal();
  }
  tree.addEventListener('change', event => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || readonly || current?.root) return;
    const permission = target.dataset['permission'];
    if (!permission || !can(permission)) return;
    state.catalog.filter(entry => allows([permission], entry.permission) && can(entry.permission)).forEach(entry => { if (target.checked) selected.add(entry.permission); else selected.delete(entry.permission); });
    if (target.checked) state.catalog.forEach(entry => { if (selected.has(entry.permission) && entry.requires) selected.add(entry.requires); });
    else state.catalog.forEach(entry => { if (entry.requires && !selected.has(entry.requires)) selected.delete(entry.permission); });
    renderTree();
  }, { signal: listeners.signal });
  async function save(): Promise<void> {
    if (readonly || !can('auth.' + (current ? 'update.' : 'create.') + kind)) return;
    const name = input(doc, 'authName').value.trim();
    if (!name || /[\u0000-\u001f\u007f]/.test(name)) { toast(t('auth_invalid_name'), 'error'); return; }
    const permissions = current?.root ? ['*'] : foldPermissions(selected, state.catalog, can);
    if (!current?.root && !permissions.every(can)) return;
    const body: Record<string, unknown> = { kind, name, permissions };
    if (current) body['id'] = current.id;
    if (kind === 'accounts') { const password = input(doc, 'authPassword').value; if (password || !current) body['password'] = password; }
    else if (current) body['rotate'] = checkbox(doc, 'authRotate').checked;
    await withBusy(button(doc, 'authSave'), async () => {
      clearError();
      const response = await api('POST', current ? '/api/auth/update' : '/api/auth/create', body);
      if (disposed) return;
      if (!response?.success || !isRecord(response.obj)) { error.textContent = response?.msg || t('error'); error.hidden = false; return; }
      dialog.close(); input(doc, 'authPassword').value = '';
      toast(t('auth_saved'), 'success');
      if (typeof response.obj['token'] === 'string') { input(doc, 'authSecret').value = response.obj['token']; element(doc, 'authRestartHint').hidden = !Array.isArray(response.obj['restart']) || !response.obj['restart'].length; secretDialog.showModal(); }
      void load();
    });
  }
  async function remove(item: Identity, trigger: HTMLButtonElement): Promise<void> {
    if (item.root || !editable(item) || !await confirm(t('auth_confirm_delete') + '\n' + item.name, t('confirm')) || disposed) return;
    await withBusy(trigger, async () => {
      const response = await api('POST', '/api/auth/delete', { id: item.id, kind: item.kind });
      if (disposed) return;
      if (!response?.success) { toast(response?.msg || t('error'), 'error'); return; }
      toast(t('deleted'), 'success'); void load();
    });
  }
  for (const kind of ['accounts', 'tokens'] as const) element(doc, 'auth' + kind + 'Table').addEventListener('click', event => {
    const target = event.target instanceof Element ? event.target.closest<HTMLButtonElement>('[data-auth-action]') : null;
    if (!target || target.disabled) return;
    const item = identities.get(kind)?.find(item => item.id === target.dataset['authId']);
    if (!item) return;
    if (target.dataset['authAction'] === 'delete') void remove(item, target); else open(kind, item, target.dataset['authAction'] === 'info', target);
  }, { signal: listeners.signal });
  button(doc, 'btnLoadAuth').addEventListener('click', () => { void load(); }, { signal: listeners.signal });
  for (const [id, kind] of [['btnCreateAccount', 'accounts'], ['btnCreateToken', 'tokens']] as const) button(doc, id).addEventListener('click', event => { if (event.currentTarget instanceof HTMLElement) open(kind, null, false, event.currentTarget); }, { signal: listeners.signal });
  form.addEventListener('submit', event => { event.preventDefault(); void save(); }, { signal: listeners.signal });
  input(doc, 'authName').addEventListener('input', clearError, { signal: listeners.signal });
  button(doc, 'authClose').addEventListener('click', () => { dialog.close(); }, { signal: listeners.signal });
  button(doc, 'authSecretClose').addEventListener('click', () => { secretDialog.close(); }, { signal: listeners.signal });
  input(doc, 'authSecret').addEventListener('click', event => { if (event.currentTarget instanceof HTMLInputElement) event.currentTarget.select(); }, { signal: listeners.signal });
  input(doc, 'authSearch').addEventListener('input', () => { render('accounts'); render('tokens'); }, { signal: listeners.signal });
  dialog.addEventListener('close', () => { input(doc, 'authPassword').value = ''; if (opener?.isConnected) opener.focus(); }, { signal: listeners.signal });
  secretDialog.addEventListener('close', () => { input(doc, 'authSecret').value = ''; }, { signal: listeners.signal });
  return () => { disposed = true; listeners.abort(); dialog.close(); secretDialog.close(); input(doc, 'authPassword').value = ''; input(doc, 'authSecret').value = ''; };
}

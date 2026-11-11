import type { Translate } from '../shared/formatting';

// Disabled controls cannot reliably receive focus or show a native tooltip.
// Keep their explanations visible and associated with the control instead.
export function initPermissionReasons(doc: Document, t: Translate) {
  const reasons = new Map<HTMLElement, HTMLElement>();
  let nextId = 0;
  let stopped = false;
  let scheduled = false;
  let translations = { missing: t('permission_missing'), noPanels: t('no_panels'), root: t('auth_root_protected') };
  function isReason(text: string): boolean {
    const [before = '', after = ''] = t('permission_missing').split('{permission}');
    return !!text && (text === t('no_panels') ||
      (before || after) !== '' && text.startsWith(before) && text.endsWith(after));
  }
  function remove(target: HTMLElement, reason: HTMLElement): void {
    const described = (target.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(id => id && id !== reason.id);
    if (described.length) target.setAttribute('aria-describedby', described.join(' '));
    else target.removeAttribute('aria-describedby');
    reason.remove();
    reasons.delete(target);
  }
  function update(): void {
    scheduled = false;
    if (stopped) return;
    const next = new Map<HTMLElement, string>();
    doc.querySelectorAll<HTMLElement>('button[disabled][title], input[disabled][title], input[readonly][title], textarea[readonly][title], select[disabled][title]').forEach(target => {
      if (!target.closest('#tab-config, #configEditorOverlay, .permission-restricted') && isReason(target.title) && (!target.closest('#usersTableWrap') || target.title === t('no_panels'))) next.set(target, target.title);
    });
    doc.querySelectorAll<HTMLSelectElement>('select').forEach(select => {
      if (select.closest('#tab-config, #configEditorOverlay')) return;
      const messages = [...new Set([...select.options].filter(option => option.disabled && isReason(option.title) && (!select.closest('#usersTableWrap') || option.title === t('no_panels'))).map(option => option.title))];
      if (messages.length) next.set(select, [next.get(select), ...messages].filter(Boolean).join(' · '));
    });
    for (const [target, reason] of reasons) if (!target.isConnected || !next.has(target)) remove(target, reason);
    for (const [target, message] of next) {
      let reason = reasons.get(target);
      if (!reason) {
        reason = doc.createElement('span');
        do { reason.id = 'permission-reason-' + String(++nextId); } while (doc.getElementById(reason.id));
        reason.className = 'permission-reason';
        const anchor = target.closest('label') ?? target;
        anchor.insertAdjacentElement('afterend', reason);
        const described = (target.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean);
        described.push(reason.id);
        target.setAttribute('aria-describedby', described.join(' '));
        reasons.set(target, reason);
      }
      if (reason.textContent !== message) reason.textContent = message;
    }
  }
  const observer = new MutationObserver(() => {
    if (!scheduled) { scheduled = true; queueMicrotask(update); }
  });
  observer.observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'readonly', 'title'] });
  update();
  return {
    refreshLanguage: (): void => {
      const previous = translations;
      translations = { missing: t('permission_missing'), noPanels: t('no_panels'), root: t('auth_root_protected') };
      const [before = '', after = ''] = previous.missing.split('{permission}');
      doc.querySelectorAll<HTMLElement>('[title]').forEach(target => {
        const title = target.title;
        if (title === previous.noPanels) target.title = translations.noPanels;
        else if (title === previous.root) target.title = translations.root;
        else if (previous.missing.includes('{permission}') && title.startsWith(before) && title.endsWith(after) && title.length >= before.length + after.length) {
          const permission = title.slice(before.length, after ? -after.length : undefined);
          target.title = translations.missing.replace('{permission}', permission);
        }
      });
      update();
    },
    dispose: (): void => {
      stopped = true;
      observer.disconnect();
      for (const [target, reason] of reasons) remove(target, reason);
    },
  };
}

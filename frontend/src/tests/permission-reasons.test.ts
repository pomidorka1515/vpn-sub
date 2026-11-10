import { afterEach, describe, expect, it, vi } from 'vitest';
import { initPermissionReasons } from '../admin/permission-reasons';
import { required } from './required';

let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); dispose = undefined; });
const t = (key: string): string => key === 'permission_missing' ? 'Missing {permission} permission' : key;

describe('permission explanations', () => {
  it('explains dynamic disabled controls and preserves existing descriptions', async () => {
    document.body.innerHTML = '<div id="actions"><span id="existing"></span></div>';
    dispose = initPermissionReasons(document, t).dispose;
    const button = document.createElement('button');
    button.disabled = true; button.title = 'Missing code.delete permission';
    button.setAttribute('aria-describedby', 'existing');
    required(document.getElementById('actions')).append(button);
    await vi.waitFor(() => { expect(button.nextElementSibling?.textContent).toBe('Missing code.delete permission'); });
    const reason = required(button.nextElementSibling);
    expect(button.getAttribute('aria-describedby')).toBe('existing ' + reason.id);
    button.disabled = false;
    await vi.waitFor(() => { expect(button.nextElementSibling).toBeNull(); });
    expect(button.getAttribute('aria-describedby')).toBe('existing');
  });
  it('explains disabled options without changing checkbox markup', async () => {
    document.body.innerHTML = '<label><input type="checkbox" disabled title="no_panels"><span class="track"></span></label><select><option disabled title="Missing code.add.bonus permission">bonus</option><option>register</option></select>';
    dispose = initPermissionReasons(document, t).dispose;
    const checkbox = required(document.querySelector<HTMLInputElement>('input'));
    expect(checkbox.nextElementSibling?.className).toBe('track');
    expect(checkbox.closest('label')?.nextElementSibling?.textContent).toBe('no_panels');
    const select = required(document.querySelector('select'));
    expect(select.nextElementSibling?.textContent).toBe('Missing code.add.bonus permission');
    expect(select.getAttribute('aria-describedby')).toBe(select.nextElementSibling?.id);
    required(select.querySelector('option')).disabled = false;
    await vi.waitFor(() => { expect(select.nextElementSibling).toBeNull(); });
  });
  it('translates explanations on dynamically rendered controls', () => {
    document.body.innerHTML = '<button disabled title="Missing code.delete permission"></button>';
    let language = 'en';
    const translate = (key: string): string => key === 'permission_missing' ? language === 'en' ? 'Missing {permission} permission' : 'Required {permission}' : key;
    const reasons = initPermissionReasons(document, translate); dispose = reasons.dispose;
    const button = required(document.querySelector('button'));
    language = 'ru'; reasons.refreshLanguage();
    expect(button.title).toBe('Required code.delete');
    expect(button.nextElementSibling?.textContent).toBe('Required code.delete');
  });
  it('omits root protection and users table permission explanations without suppressing other reasons', async () => {
    document.body.innerHTML = '<div id="usersTableWrap"><button disabled title="Missing user.delete permission"></button></div><div id="auth"><button disabled title="auth_root_protected"></button></div><div class="toolbar"><button disabled title="Missing health.sync permission"></button><button>next</button></div>';
    dispose = initPermissionReasons(document, t).dispose;
    expect(document.querySelector('#usersTableWrap .permission-reason')).toBeNull();
    expect(document.querySelector('#auth .permission-reason')).toBeNull();
    const button = required(document.querySelector<HTMLButtonElement>('.toolbar button'));
    expect(button.nextElementSibling?.textContent).toBe('Missing health.sync permission');
    expect(button.getAttribute('aria-describedby')).toBe(button.nextElementSibling?.id);
    const userButton = required(document.querySelector<HTMLButtonElement>('#usersTableWrap button'));
    userButton.title = 'no_panels';
    await vi.waitFor(() => { expect(userButton.nextElementSibling?.textContent).toBe('no_panels'); });
  });
});

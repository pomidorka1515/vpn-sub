import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMainEditor } from '../admin/config/main';
import { ConfigNumber, stringifyConfig } from '../admin/config/json';
import type { ConfigValue } from '../admin/config/json';
import { editor, OPTIONAL_KEYS, RESTART_KEYS } from '../admin/config/state';
import { validateDraft } from '../admin/config/validation';

afterEach(() => {
  editor.draft = null;
  editor.loaded = null;
  editor.jsonInputs.clear();
});

const discordIssues = (discord: ConfigValue) => validateDraft({ discord }).filter(issue => issue.path === 'discord');

describe('Discord config', () => {
  it('is optional and requires restart when changed', () => {
    expect(OPTIONAL_KEYS).toContain('discord');
    expect(RESTART_KEYS).toContain('discord');
    expect(validateDraft({}).filter(issue => issue.path === 'discord')).toEqual([]);
    expect(discordIssues({ token: '', whitelist: [] })).toEqual([]);
    expect(discordIssues({ token: 'token', whitelist: [new ConfigNumber('123456789012345678')] })).toEqual([]);
  });

  it.each([
    null, [], 'token', {}, { token: 1, whitelist: [] }, { token: '' },
    { token: '', whitelist: [0] }, { token: '', whitelist: [-1] },
    { token: '', whitelist: ['123'] }, { token: '', whitelist: [1.5] },
    { token: '', whitelist: [new ConfigNumber('123'), new ConfigNumber('123')] },
    { token: '', whitelist: [], extra: true },
  ])('rejects invalid Discord config: %j', value => {
    expect(discordIssues(value).length).toBeGreaterThan(0);
  });

  it('adds, edits and removes Discord without rounding user IDs', () => {
    document.body.innerHTML = '<div id="config-pane-main"></div>';
    editor.draft = {};
    const dirty = vi.fn();
    createMainEditor(dirty, key => key).renderMain();
    expect(document.querySelector('#cfg-box-discord .config-restart-icon')).not.toBeNull();
    document.querySelector<HTMLButtonElement>('[data-optional-add="discord"]')!.click();
    expect(editor.draft.discord).toEqual({ token: '', whitelist: [] });
    const token = document.getElementById('cfg-discord-token') as HTMLInputElement;
    expect(token.type).toBe('password');
    token.value = 'shared-token';
    token.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLButtonElement>('[data-add="cfg-rows-discord"]')!.click();
    const uid = document.querySelector<HTMLInputElement>('#cfg-rows-discord input')!;
    uid.value = '123456789012345678';
    uid.dispatchEvent(new Event('input', { bubbles: true }));
    expect(stringifyConfig(editor.draft.discord!)).toBe('{"token":"shared-token","whitelist":[123456789012345678]}');
    expect(discordIssues(editor.draft.discord!)).toEqual([]);
    document.querySelector<HTMLButtonElement>('[data-optional-remove="discord"]')!.click();
    expect(Object.hasOwn(editor.draft, 'discord')).toBe(false);
    expect(dirty).toHaveBeenCalled();
  });
});

import { required, requiredInstance } from './required';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createCardEditor } from '../admin/config/cards';
import { cssId } from '../admin/config/controls';
import { stringifyConfig } from '../admin/config/json';
import { editor } from '../admin/config/state';

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.(); dispose = undefined;
  editor.draft = null;
  editor.open.inbounds.clear(); editor.open.panels.clear();
  editor.headerRows.clear(); editor.jsonInputs.clear();
});

function setup() {
  document.body.innerHTML = '<div id="tab-config"><div id="config-pane-inbounds"></div><div id="config-pane-panels"></div></div>';
  editor.draft = {
    nodes: { local: 'localhost' },
    profiles: { first: { flag: '🏳', name: ['First', 'Первый'], node: 'missing', json: { valid: true }, xhttpExtra: {} }, second: {} },
    '3xui': { first: { name: 'Panel', port: 2053, mode: 'blacklist', inbounds_list: [] }, second: { name: 'Other' } },
  };
  const markDirty = vi.fn(); const openNew = vi.fn();
  const confirm = vi.fn<(message: string, title: string) => Promise<boolean | string | null>>();
  const cards = createCardEditor({ markDirty, openNew, confirm, t: key => key, language: () => 'ru' });
  dispose = cards.init();
  cards.renderCards('inbounds'); cards.renderCards('panels');
  const card = (kind: string, id: string) => required(document.querySelector<HTMLElement>(`[data-kind="${kind}"][data-id="${id}"]`));
  const input = (kind: string, id: string, field: string) => required(card(kind, id).querySelector<HTMLInputElement>(`[data-field="${field}"]`));
  const edit = (element: HTMLInputElement | HTMLTextAreaElement, value: string) => { element.value = value; element.dispatchEvent(new Event('input', { bubbles: true })); };
  return { cards, card, input, edit, markDirty, openNew, confirm };
}

describe('configuration cards', () => {
  it('opens cards from headers and buttons and invokes the new-card action', () => {
    const { card, openNew } = setup();
    const profile = card('inbounds', 'first');
    expect(required(profile.querySelector('.panel-card-title')).textContent).toContain('Первый');
    expect((requiredInstance(profile.querySelector('[data-field="node"]'), HTMLSelectElement)).value).toBe('missing');
    required(profile.querySelector<HTMLElement>('.panel-card-title')).click();
    expect(profile.classList.contains('open')).toBe(true);
    expect(editor.open.inbounds.has('first')).toBe(true);
    const toggle = required(profile.querySelector<HTMLButtonElement>('.config-card-toggle'));
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    toggle.click();
    expect(profile.classList.contains('open')).toBe(false);
    expect(editor.open.inbounds.has('first')).toBe(false);
    required(document.getElementById('cfg-new-panels')).click();
    expect(openNew).toHaveBeenCalledWith('panels');
  });

  it('retains invalid JSON and invalid pending renames across renders, then moves them on rename', () => {
    const { cards, card, input, edit, markDirty } = setup();
    const json = required(card('inbounds', 'first').querySelector<HTMLTextAreaElement>('[data-field="json"]'));
    edit(json, '{broken');
    const id = input('inbounds', 'first', 'id');
    edit(id, 'second'); id.dispatchEvent(new Event('blur'));
    expect(Object.keys(required(required(editor.draft)['profiles']))).toEqual(['first', 'second']);
    cards.renderCards('inbounds');
    expect(input('inbounds', 'first', 'id').value).toBe('second');
    expect(required(card('inbounds', 'first').querySelector<HTMLTextAreaElement>('[data-field="json"]')).value).toBe('{broken');
    const renamed = input('inbounds', 'first', 'id');
    edit(renamed, ' renamed '); renamed.dispatchEvent(new Event('blur'));
    expect(Object.keys(required(required(editor.draft)['profiles']))).toEqual(['renamed', 'second']);
    expect(editor.jsonInputs.get(JSON.stringify(['renamed', 'json']))).toBe('{broken');
    expect(editor.jsonInputs.has(JSON.stringify(['first', 'json']))).toBe(false);
    expect(required(card('inbounds', 'renamed').querySelector<HTMLTextAreaElement>('[data-field="json"]')).value).toBe('{broken');
    expect(markDirty).toHaveBeenCalled();
  });

  it('edits panel rows and headers, preserving duplicate rows through rename', () => {
    const { card, input, edit } = setup();
    const panelId = cssId('first');
    required(card('panels', 'first').querySelector<HTMLButtonElement>(`[data-add="cfg-pn-rows-${panelId}"]`)).click();
    const row = required(document.querySelector<HTMLInputElement>(`#cfg-pn-rows-${panelId} input`));
    edit(row, '9007199254740993');
    expect(stringifyConfig(required(required(editor.draft)['3xui']))).toContain('9007199254740993');
    edit(row, 'not-an-integer');
    expect(stringifyConfig(required(required(editor.draft)['3xui']))).toContain('"not-an-integer"');
    required(required(row.parentElement).querySelector<HTMLButtonElement>('[data-remove]')).click();
    expect(document.querySelectorAll(`#cfg-pn-rows-${panelId} input`)).toHaveLength(0);
    required(card('panels', 'first').querySelector<HTMLButtonElement>('[data-nested-add="inject_headers"]')).click();
    const addHeader = () => { required(card('panels', 'first').querySelector<HTMLButtonElement>(`[data-add="cfg-pn-headers-${panelId}"]`)).click(); };
    addHeader();
    let inputs = document.querySelectorAll<HTMLInputElement>(`#cfg-pn-headers-${panelId} input`);
    edit(required(inputs[0]), 'X-Test'); edit(required(inputs[1]), 'one');
    addHeader();
    inputs = document.querySelectorAll<HTMLInputElement>(`#cfg-pn-headers-${panelId} input`);
    edit(required(inputs[2]), 'X-Test'); edit(required(inputs[3]), 'two');
    const id = input('panels', 'first', 'id');
    edit(id, 'renamed'); id.dispatchEvent(new Event('blur'));
    expect(editor.headerRows.get('renamed')).toEqual([['X-Test', 'one'], ['X-Test', 'two']]);
    expect(editor.headerRows.has('first')).toBe(false);
    required(card('panels', 'renamed').querySelector<HTMLButtonElement>('[data-nested-remove="inject_headers"]')).click();
    expect(editor.headerRows.has('renamed')).toBe(false);
    expect(stringifyConfig(required(required(editor.draft)['3xui']))).not.toContain('inject_headers');
  });

  it('updates panel values and confirms deletion before removing draft state', async () => {
    const { card, input, edit, confirm, markDirty } = setup();
    edit(input('panels', 'first', 'port'), '1234');
    expect(stringifyConfig(required(required(editor.draft)['3xui']))).toContain('"port":1234');
    edit(input('panels', 'first', 'port'), 'invalid');
    expect(stringifyConfig(required(required(editor.draft)['3xui']))).toContain('"port":"invalid"');
    const checkbox = input('panels', 'first', 'https');
    checkbox.checked = true; checkbox.dispatchEvent(new Event('change', { bubbles: true }));
    expect(stringifyConfig(required(required(editor.draft)['3xui']))).toContain('"https":true');
    confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const remove = () => { required(card('panels', 'first').querySelector<HTMLButtonElement>('.config-card-delete')).click(); };
    editor.headerRows.set('first', [['x', 'y']]); editor.open.panels.add('first');
    remove();
    await vi.waitFor(() => { expect(confirm).toHaveBeenCalledTimes(1); });
    expect(card('panels', 'first')).not.toBeNull();
    remove();
    await vi.waitFor(() => { expect(document.querySelector('[data-kind="panels"][data-id="first"]')).toBeNull(); });
    expect(editor.headerRows.has('first')).toBe(false);
    expect(editor.open.panels.has('first')).toBe(false);
    expect(markDirty).toHaveBeenCalled();
  });
});

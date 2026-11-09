import { afterEach, describe, expect, it, vi } from 'vitest';
import { createConfigDialog } from '../admin/config/dialog';
import { editor } from '../admin/config/state';
import { stringifyConfig } from '../admin/config/json';

afterEach(() => { editor.draft = null; editor.dialog = null; document.body.style.overflow = ''; });
function setup() {
  document.body.innerHTML = '<button id="cfg-new-inbounds"></button><button id="cfg-new-panels"></button><div id="configEditorOverlay" hidden><h2 id="configEditorTitle"></h2><div id="configEditorBody"></div><button id="configEditorCancel"></button><button id="configEditorOk"></button></div>';
  editor.draft = { nodes: { node: 'example.com' }, profiles: {}, '3xui': {} };
  const markDirty = vi.fn(); const renderCards = vi.fn();
  const open = createConfigDialog({ markDirty, renderCards });
  const field = (key: string) => document.querySelector<HTMLInputElement>(`#configEditorBody [data-field="${key}"]`)!;
  const submit = () => document.getElementById('configEditorOk')!.click();
  return { open, field, submit, markDirty, renderCards };
}

describe('new configuration cards', () => {
  it('requires a unique panel id and validates values before mutating the draft', () => {
    const { open, field, submit, markDirty, renderCards } = setup();
    open('panels'); submit();
    expect(document.getElementById('cfg-dialog-err')!.textContent).toBe('config_err_required');
    field('id').value = 'panel'; field('port').value = '65536';
    (document.getElementById('cfg-dlg-token') as HTMLInputElement).value = 'x'.repeat(20);
    submit();
    expect(document.getElementById('cfg-dialog-err')!.textContent).toBe('config_err_panel_port');
    expect(editor.draft!['3xui']).toEqual({});
    field('port').value = '2053'; field('name').value = 'Panel'; submit();
    expect(stringifyConfig(editor.draft!['3xui']!)).toContain('"port":2053');
    expect(markDirty).toHaveBeenCalledOnce();
    expect(renderCards).toHaveBeenCalledWith('panels');
    expect(document.getElementById('configEditorOverlay')!.hidden).toBe(true);
    open('panels'); field('id').value = 'panel'; submit();
    expect(document.getElementById('cfg-dialog-err')!.textContent).toBe('config_err_unique');
    open.dispose();
  });
  it('creates inbound JSON without losing large integers and rejects malformed JSON', () => {
    const { open, field, submit, markDirty } = setup();
    open('inbounds');
    field('id').value = 'profile'; field('json').value = '{bad';
    submit();
    expect(document.getElementById('cfg-dialog-err')!.textContent).toBe('config_err_json');
    expect(editor.draft!.profiles).toEqual({});
    field('json').value = '{"id":9007199254740993}'; submit();
    expect(stringifyConfig(editor.draft!.profiles!)).toContain('9007199254740993');
    expect(markDirty).toHaveBeenCalledOnce();
  });
  it('restores focus and scroll state on cancellation or replacement', () => {
    const { open } = setup();
    document.body.style.overflow = 'auto';
    const opener = document.getElementById('cfg-new-panels')!; opener.focus();
    open('panels');
    expect(document.body.style.overflow).toBe('hidden');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(document.body.style.overflow).toBe('auto');
    expect(document.activeElement).toBe(opener);
    expect(editor.dialog).toBeNull();
    open('inbounds'); open('panels'); open.dispose();
    expect(document.body.style.overflow).toBe('auto');
    expect(document.getElementById('configEditorOverlay')!.hidden).toBe(true);
  });
});

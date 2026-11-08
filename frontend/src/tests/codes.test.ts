import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initCodes } from '../admin/codes';
import type { AdminContext } from '../admin/context';
import type { ApiResult } from '../shared/api';

const success = (obj: unknown = null): ApiResult => ({ success: true, obj, status: 200 });
const details = { action: 'bonus', days: 30, gb: 10, wl_gb: 5, uses: 1, perma: false };
let dispose: (() => void) | undefined;
afterEach(() => { dispose?.(); });

function setup() {
  const template = readFileSync('src/templates/admin.html', 'utf8');
  document.body.innerHTML = template.slice(template.indexOf('<div id="tab-codes"'), template.indexOf('<div id="tab-logs"'));
  const api = vi.fn<AdminContext['api']>();
  const toast = vi.fn();
  const confirm = vi.fn<AdminContext['confirm']>().mockResolvedValue(true);
  const clipboard = { writeText: vi.fn<Clipboard['writeText']>().mockResolvedValue() };
  dispose = initCodes({ doc: document, t: key => key, api, toast, confirm, loadGen: () => () => true }, clipboard);
  const list = document.getElementById('codeList')!;
  const click = (id: string) => document.getElementById(id)!.click();
  const cards = () => [...list.querySelectorAll<HTMLElement>('.code-card')];
  const load = async (names = ['FIRST', 'SECOND']) => {
    api.mockResolvedValueOnce(success(names));
    for (const _name of names) api.mockResolvedValueOnce(success(details));
    click('btnLoadCodes');
    await vi.waitFor(() => expect(document.getElementById('btnLoadCodes')!.hasAttribute('aria-busy')).toBe(false));
  };
  return { api, toast, confirm, clipboard, list, click, cards, load };
}

describe('admin codes', () => {
  it('shows the loaded empty state once and leaves it untouched on refresh', async () => {
    const { load, list } = setup();
    await load([]);
    const empty = list.querySelector('.code-empty');
    expect(empty!.textContent).toContain('no_codes');
    await load([]);
    expect(list.querySelector('.code-empty')).toBe(empty);
  });

  it('makes an unchanged refresh a DOM no-op, including while fetching', async () => {
    const { load, list, cards, api, click } = setup();
    await load();
    const original = cards();
    const copy = original[0]!.querySelector<HTMLButtonElement>('button')!;
    copy.focus();
    const mutations = vi.fn();
    const observer = new MutationObserver(mutations);
    observer.observe(list, { childList: true, subtree: true, characterData: true, attributes: true });
    let resolve!: (value: ApiResult) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    api.mockResolvedValue(success(details));
    click('btnLoadCodes');
    expect(cards()).toEqual(original);
    expect(list.querySelector('.loading')).toBeNull();
    resolve(success(['FIRST', 'SECOND']));
    await vi.waitFor(() => expect(document.getElementById('btnLoadCodes')!.hasAttribute('aria-busy')).toBe(false));
    expect(cards()[0]).toBe(original[0]);
    expect(cards()[1]).toBe(original[1]);
    expect(document.activeElement).toBe(copy);
    expect(mutations).not.toHaveBeenCalled();
    observer.disconnect();
  });

  it('removes only the deleted code without reloading and handles the final deletion', async () => {
    const { load, cards, api, list } = setup();
    await load();
    const [first, second] = cards();
    api.mockClear().mockResolvedValue(success());
    first!.querySelector<HTMLButtonElement>('[data-code-action="delete"]')!.click();
    await vi.waitFor(() => expect(cards()).toHaveLength(1));
    expect(cards()[0]).toBe(second);
    expect(api).toHaveBeenCalledExactlyOnceWith('POST', '/api/code/delete', { code: 'FIRST' });
    expect(document.getElementById('codeCount')!.textContent).toBe('1');
    second!.querySelector<HTMLButtonElement>('[data-code-action="delete"]')!.click();
    await vi.waitFor(() => expect(cards()).toHaveLength(0));
    expect(list.querySelector('.code-empty')).not.toBeNull();
    expect(document.getElementById('codeCount')!.textContent).toBe('0');
  });

  it('preserves the list after failed refreshes and failed deletions', async () => {
    const { load, api, click, cards, toast } = setup();
    await load();
    const first = cards()[0]!;
    api.mockResolvedValue({ success: false, msg: 'offline', status: 500 });
    click('btnLoadCodes');
    await vi.waitFor(() => expect(toast).toHaveBeenCalledWith('offline', 'error'));
    first.querySelector<HTMLButtonElement>('[data-code-action="delete"]')!.click();
    await vi.waitFor(() => expect(api).toHaveBeenCalledWith('POST', '/api/code/delete', { code: 'FIRST' }));
    expect(cards()[0]).toBe(first);
    expect(cards()).toHaveLength(2);
  });

  it('updates changed details without replacing unaffected cards or action buttons', async () => {
    const { load, api, click, cards } = setup();
    await load();
    const [first, second] = cards();
    const actions = first!.querySelector('.code-actions');
    api.mockResolvedValueOnce(success(['FIRST', 'SECOND', 'NEW']));
    api.mockResolvedValueOnce(success({ ...details, uses: 7 }));
    api.mockResolvedValue(success(details));
    click('btnLoadCodes');
    await vi.waitFor(() => expect(cards()).toHaveLength(3));
    expect(cards()[0]).toBe(first);
    expect(cards()[1]).toBe(second);
    expect(first!.querySelector('.code-actions')).toBe(actions);
    expect(first!.querySelector('.code-usage')!.textContent).toContain('7');
  });

  it('does not resurrect a deleted code from an in-flight refresh', async () => {
    const { load, api, click, cards } = setup();
    await load();
    let resolve!: (value: ApiResult) => void;
    api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    api.mockResolvedValue(success());
    click('btnLoadCodes');
    cards()[0]!.querySelector<HTMLButtonElement>('[data-code-action="delete"]')!.click();
    await vi.waitFor(() => expect(cards()).toHaveLength(1));
    resolve(success(['FIRST', 'SECOND']));
    await vi.waitFor(() => expect(document.getElementById('btnLoadCodes')!.hasAttribute('aria-busy')).toBe(false));
    expect(cards()).toHaveLength(1);
    expect(cards()[0]!.textContent).toContain('SECOND');
  });

  it('adds a code locally and keeps copy working for escaped names', async () => {
    const { load, api, click, cards, clipboard } = setup();
    await load([]);
    click('btnShowAddCode');
    const name = '<gift&"special>';
    (document.getElementById('addCode') as HTMLInputElement).value = name;
    api.mockClear().mockResolvedValue(success());
    click('btnDoAddCode');
    await vi.waitFor(() => expect(cards()).toHaveLength(1));
    expect(api).toHaveBeenCalledTimes(1);
    expect(cards()[0]!.querySelector('.code-name')!.textContent).toBe(name);
    cards()[0]!.querySelector<HTMLButtonElement>('[data-code-action="copy"]')!.click();
    await vi.waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith(name));
  });
});

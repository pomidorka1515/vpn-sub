import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initAdmin } from '../admin/main';
import { editor } from '../admin/config/state';

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.(); dispose = undefined;
  editor.fetched = false; editor.loaded = null; editor.draft = null; editor.dirty = false;
});
function setup(request: typeof fetch) {
  const html = readFileSync('src/templates/admin.html', 'utf8');
  document.body.innerHTML = html.slice(html.indexOf('<body>') + 6, html.indexOf('</body>'));
  const bootstrap = document.createElement('script'); bootstrap.type = 'application/json'; bootstrap.id = 'frontend-bootstrap';
  bootstrap.textContent = JSON.stringify({ prefix: '/sub', language: 'en', translations: {}, fallback: {}, page: { chartUrl: '/chart.js', debounceMs: 20 } });
  document.head.append(bootstrap);
  vi.stubGlobal('fetch', request);
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
  dispose = initAdmin();
}
const token = () => Response.json({ success: true, obj: { token: 'csrf', api_root: '/api', panels_configured: true } });

describe('admin application integration', () => {
  it('waits for authentication before refreshing the selected tab and attaches authorization', async () => {
    let resolve!: (response: Response) => void;
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(done => { resolve = done; })).mockResolvedValue(Response.json({ success: true, obj: [] }));
    setup(request);
    document.querySelector<HTMLElement>('[data-tab="users"]')!.click();
    expect(request).toHaveBeenCalledTimes(1);
    resolve(token());
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith('https://example.test/api/api/user/list', expect.objectContaining({ method: 'GET', headers: { Authorization: 'csrf' } })));
    expect(document.getElementById('bootOverlay')!.hidden).toBe(true);
    expect(document.getElementById('tab-users')!.classList.contains('active')).toBe(true);
    await vi.waitFor(() => expect(document.getElementById('usersTableWrap')!.textContent).toContain('no_users'));
  });
  it('prevents pending authentication from activating a disposed application', async () => {
    let resolve!: (response: Response) => void;
    const request = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    setup(request);
    dispose?.(); dispose = undefined;
    resolve(token());
    await Promise.resolve(); await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(1);
    expect(document.getElementById('bootOverlay')!.hidden).toBe(false);
  });
});

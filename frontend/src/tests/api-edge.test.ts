import { required } from './required';
import { describe, expect, it, vi } from 'vitest';
import { createApi, serializeBody } from '../shared/api';
import { createHistoryService } from '../history/service';

const t = (key: string) => key;

describe('API transport edge cases', () => {
  it('uses custom serialization and rejects non-string custom output before sending', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    const api = createApi(() => ({ baseUrl: '/base///', headers: () => ({ Authorization: 'secret' }) }), t, vi.fn(), request);
    const body = { [Symbol.for('api.serialize')]() { return '{"large":9007199254740993}'; } };
    expect(await api('POST', '/config', body)).toEqual({ success: true, status: 204 });
    expect(request).toHaveBeenCalledWith('/base/config', expect.objectContaining({ body: '{"large":9007199254740993}', headers: { Authorization: 'secret', 'Content-Type': 'application/json' } }));
    expect(() => serializeBody({ [Symbol.for('api.serialize')]: () => 123 })).toThrow();
  });
  it('blocks guarded requests and supports dynamic network errors', async () => {
    const request = vi.fn<typeof fetch>().mockRejectedValue(new Error('offline'));
    const toast = vi.fn();
    const guarded = createApi(() => ({ guard: () => true, guardMessage: () => 'blocked' }), t, toast, request);
    expect(await guarded('GET', '/')).toBeNull();
    expect(request).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith('blocked', 'error');
    const offline = createApi(() => ({ networkError: 'toast', networkMessage: () => 'offline' }), t, toast, request);
    expect(await offline('GET', '/')).toBeNull();
    expect(toast).toHaveBeenCalledWith('offline', 'error');
    const rethrow = createApi(() => ({}), t, toast, request);
    await expect(rethrow('GET', '/')).rejects.toThrow('offline');
  });
  it('retains raw JSON and ETag while handling malformed responses and allowed 401s', async () => {
    const on401 = vi.fn();
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response('{bad', { status: 502 })).mockResolvedValueOnce(Response.json({ success: false, msg: null }, { status: 401, headers: { ETag: '"v1"' } }));
    const api = createApi(() => ({ on401 }), t, vi.fn(), request);
    const malformed = await api('GET', '/');
    expect(malformed).toMatchObject({ success: false, msg: 'invalid_response', status: 502 });
    expect(malformed?.rawJson).toBe('{bad');
    const allowed = await api('GET', '/', undefined, { allow401: () => true });
    expect(allowed?.etag).toBe('"v1"');
    expect(Object.keys(required(allowed))).not.toContain('rawJson');
    expect(on401).not.toHaveBeenCalled();
  });
});

describe('history service', () => {
  it('passes through validated samples and refuses malformed successful payloads', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json({ success: true, obj: [{ ts: 0, up: 1, down: 2, wl_up: 3, wl_down: 4 }] })).mockResolvedValueOnce(Response.json({ success: true, obj: [{ ts: 'bad' }] }));
    const service = createHistoryService({ baseUrl: '/webapi', request, translate: t, toast: vi.fn(), onUnauthorized: vi.fn() });
    expect((await service.history(7)).samples).toEqual([{ ts: 0, up: 1, down: 2, wl_up: 3, wl_down: 4 }]);
    expect(request).toHaveBeenCalledWith('/webapi/history?days=7', expect.anything());
    expect((await service.history(1)).samples).toBeNull();
  });
  it('handles unauthorized and offline requests without treating them as samples', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json({ success: false }, { status: 401 })).mockRejectedValueOnce(new Error('offline'));
    const onUnauthorized = vi.fn(); const toast = vi.fn();
    const service = createHistoryService({ baseUrl: '', request, translate: t, toast, onUnauthorized });
    expect(await service.history(1)).toEqual({ response: null, samples: null });
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(await service.history(1)).toEqual({ response: null, samples: null });
    expect(toast).toHaveBeenCalledWith('network_err', 'error');
  });
});

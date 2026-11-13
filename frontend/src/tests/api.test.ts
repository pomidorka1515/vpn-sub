import { describe, expect, it, vi } from 'vitest';
import { createApi, parseApiEnvelope } from '../shared/api';
import { sharedMessage } from '../shared/messages';

const translate = (key: string): string => key;

describe('parseApiEnvelope', () => {
  it.each([
    { success: true, msg: null, obj: null },
    { success: true, obj: {} },
    { success: false, msg: 'Invalid credentials', obj: null },
  ])('accepts an envelope with message $msg', (envelope) => {
    expect(parseApiEnvelope(envelope)).toBe(envelope);
  });

  it.each([null, [], { success: 1 }, { msg: false }, { msg: 1 }, { msg: {} }, { msg: [] }])(
    'rejects malformed envelope %j',
    (envelope) => {
      expect(() => parseApiEnvelope(envelope)).toThrow(sharedMessage('invalid_response'));
    },
  );
});

describe('createApi', () => {
  it.each([
    { method: 'POST', path: '/login', obj: null },
    { method: 'POST', path: '/register', obj: null },
    { method: 'POST', path: '/logout', obj: null },
    { method: 'GET', path: '/fingerprints', obj: ['chrome'] },
    { method: 'GET', path: '/bandwidth_history', obj: [] },
  ])('preserves a null message for $method $path', async ({ method, path, obj }) => {
    const envelope = { success: true, msg: null, obj };
    const request = vi.fn<typeof fetch>().mockResolvedValue(Response.json(envelope));
    const toast = vi.fn<(message: string, type: string) => void>();
    const api = createApi(() => ({ baseUrl: '/sub/webapi' }), translate, toast, request);

    const result = await api(method, path);

    expect(request).toHaveBeenCalledExactlyOnceWith('/sub/webapi' + path, {
      method,
      credentials: 'same-origin',
      headers: {},
    });
    expect(result).toEqual({ ...envelope, status: 200 });
    expect(toast).not.toHaveBeenCalled();
  });

  it('does not let null messages bypass unauthorized handling', async () => {
    const on401 = vi.fn<() => void>();
    const toast = vi.fn<(message: string, type: string) => void>();
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ success: false, msg: null, obj: null }, { status: 401 }));
    const api = createApi(() => ({ on401 }), translate, toast, request);

    expect(await api('GET', '/stats')).toBeNull();
    expect(on401).toHaveBeenCalledExactlyOnceWith();
    expect(toast).not.toHaveBeenCalled();
  });
});

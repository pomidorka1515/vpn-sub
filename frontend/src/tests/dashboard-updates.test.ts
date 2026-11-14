import { afterEach, expect, it, vi } from 'vitest';
import { startDashboardUpdates } from '../dashboard/updates';

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  dispose = undefined;
  vi.useRealTimers();
});

function setup() {
  vi.useFakeTimers();
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  const cancel = vi.fn();
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        stream = controller;
      },
      cancel,
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  );
  const request = vi.fn<typeof fetch>().mockResolvedValue(response);
  const onStats = vi.fn();
  const onUnauthorized = vi.fn();
  dispose = startDashboardUpdates({
    url: '/sub/webapi/updates',
    request,
    schedule: window.setTimeout.bind(window),
    cancel: (id) => {
      window.clearTimeout(id);
    },
    onStats,
    onUnauthorized,
  });
  const send = (data: string) => {
    stream.enqueue(new TextEncoder().encode(data));
  };
  return { stream, request, onStats, onUnauthorized, send, cancel };
}

it('ignores comments and invalid data, and reconnects two seconds after EOF', async () => {
  const { stream, request, onStats, send } = setup();
  send(': ping\n\ndata: {broken}\n\ndata: {}\n\n');
  await vi.advanceTimersByTimeAsync(2000);
  expect(onStats).not.toHaveBeenCalled();
  request.mockResolvedValueOnce(new Response(null, { status: 503 }));
  stream.close();
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(1999);
  expect(request).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(request).toHaveBeenCalledTimes(2);
});

it('stops and reports revoked authentication instead of retrying', async () => {
  const { stream, request, onUnauthorized } = setup();
  request.mockResolvedValueOnce(new Response(null, { status: 401 }));
  stream.close();
  await vi.advanceTimersByTimeAsync(2000);
  expect(onUnauthorized).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(10000);
  expect(request).toHaveBeenCalledTimes(2);
});

it('cancels the reader, request, render and reconnect timers on disposal', async () => {
  const { request, cancel } = setup();
  await vi.advanceTimersByTimeAsync(0);
  dispose?.();
  expect(cancel).toHaveBeenCalledOnce();
  expect(request.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(10000);
  expect(request).toHaveBeenCalledTimes(1);
});

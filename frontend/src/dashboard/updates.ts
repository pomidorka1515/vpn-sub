import { parseStats } from './data';
import type { Stats } from './data';

const UPDATE_INTERVAL = 2000;

/** One stream, bounded buffering, and a fixed two-second render/reconnect cadence. */
export function startDashboardUpdates(options: {
  url: string;
  request: typeof fetch;
  schedule: (callback: () => void, delay: number) => number;
  cancel: (id: number | undefined) => void;
  onStats: (stats: Stats) => void;
  onUnauthorized: () => void;
}): () => void {
  const abort = new AbortController();
  let stopped = false;
  let pending: Stats | null = null;
  let renderTimer: number | undefined;
  let reconnectTimer: number | undefined;
  let activeReader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const isStopped = () => stopped;

  function render() {
    if (stopped) return;
    if (pending) {
      const stats = pending;
      pending = null;
      options.onStats(stats);
    }
    renderTimer = options.schedule(render, UPDATE_INTERVAL);
  }

  async function connect() {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await options.request(options.url, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'text/event-stream' },
        signal: abort.signal,
      });
      if (isStopped()) {
        await response.body?.cancel();
        return;
      }
      if (response.status === 401) {
        stop();
        options.onUnauthorized();
        return;
      }
      if (!response.ok || !response.body || !response.headers.get('Content-Type')?.startsWith('text/event-stream'))
        return;
      reader = response.body.getReader();
      activeReader = reader;
      const decoder = new TextDecoder();
      let buffer = '';
      while (!isStopped()) {
        const { done, value } = await reader.read();
        if (done || isStopped()) break;
        buffer += decoder.decode(value, { stream: true });
        let end: number;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const event = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const data = event
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trimStart())
            .join('\n');
          if (!data) continue; // Heartbeats are comments, not account updates.
          try {
            const parsed: unknown = JSON.parse(data);
            const stats = parseStats(parsed);
            if (stats) pending = stats;
          } catch {
            // Ignore malformed messages without losing the last valid state.
          }
        }
        if (buffer.length > 65536) break;
      }
    } catch {
      // Transient network failures are silent; retry on the fixed cadence.
    } finally {
      await reader?.cancel().catch(() => {});
      reader?.releaseLock();
      activeReader = undefined;
      if (!stopped) reconnectTimer = options.schedule(() => void connect(), UPDATE_INTERVAL);
    }
  }

  function stop() {
    stopped = true;
    pending = null;
    abort.abort();
    void activeReader?.cancel().catch(() => {});
    options.cancel(renderTimer);
    options.cancel(reconnectTimer);
  }

  renderTimer = options.schedule(render, UPDATE_INTERVAL);
  void connect();
  return stop;
}

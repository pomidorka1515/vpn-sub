import { createApi } from '../shared/api';
import type { Translate } from '../shared/formatting';
import { parseSamples } from './data';

export function createHistoryService(options: {
  baseUrl: string;
  request: typeof fetch;
  translate: Translate;
  toast: (message: string, type: string) => void;
  onUnauthorized: () => void;
}) {
  const api = createApi(
    () => ({
      baseUrl: options.baseUrl,
      on401: options.onUnauthorized,
      networkError: 'toast',
      networkMessage: () => options.translate('network_err'),
    }),
    options.translate,
    options.toast,
    options.request,
  );
  async function history(days: number) {
    const response = await api('GET', '/history?days=' + String(days));
    return { response, samples: response?.success ? parseSamples(response.obj) : null };
  }
  return { history };
}

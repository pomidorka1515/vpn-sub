import { createApi } from '../shared/api';
import type { Translate } from '../shared/formatting';
import { parseStats } from './data';

export function createDashboardService(options: {
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
  async function stats() {
    const response = await api('GET', '/stats');
    return { response, stats: response?.success ? parseStats(response.obj) : null };
  }
  return { api, stats };
}

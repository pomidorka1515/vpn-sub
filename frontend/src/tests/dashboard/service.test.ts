import { describe, expect, it, vi } from 'vitest';
import type { Stats } from '../../dashboard/data';
import { createDashboardService } from '../../dashboard/service';

describe('dashboard service', () => {
  it('loads stats from the backend success envelope', async () => {
    const stats: Stats = {
      displayname: 'Test', link: 'https://example.test/sub?token=test',
      uuid: 'uuid', fingerprint: 'chrome', token: 'test',
      online: false, enabled: true, wl_enabled: false, time: 0,
      bandwidth: {
        total: { upload: 12, download: 34 },
        wl_total: { upload: 0, download: 0 },
        monthly: 46, wl_monthly: 0, limit: 100, wl_limit: 0,
      },
    };
    const request = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json({ success: true, msg: null, obj: stats }),
    );
    const toast = vi.fn<(message: string, type: string) => void>();
    const onUnauthorized = vi.fn<() => void>();
    const service = createDashboardService({
      baseUrl: '/sub/webapi', translate: key => key, toast, onUnauthorized, request,
    });

    const result = await service.stats();

    expect(request).toHaveBeenCalledExactlyOnceWith('/sub/webapi/stats', {
      method: 'GET', credentials: 'same-origin', headers: {},
    });
    expect(result.response).toEqual({ success: true, msg: null, obj: stats, status: 200 });
    expect(result.stats).toEqual(stats);
    expect(toast).not.toHaveBeenCalled();
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

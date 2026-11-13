import { isRecord } from '../shared/json';

export interface Stats {
  displayname: string;
  link: string;
  uuid: string;
  fingerprint: string;
  token: string;
  online: boolean;
  enabled: boolean;
  wl_enabled: boolean;
  time: number;
  bandwidth: {
    total: Traffic;
    wl_total: Traffic;
    monthly: number;
    wl_monthly: number;
    limit: number;
    wl_limit: number;
  };
}
interface Traffic {
  upload: number;
  download: number;
}

export function parseStats(value: unknown): Stats | null {
  if (!isRecord(value) || !isRecord(value['bandwidth'])) return null;
  const b = value['bandwidth'];
  if (!isRecord(b['total']) || !isRecord(b['wl_total'])) return null;
  const total = (v: Record<string, unknown>): v is Record<string, unknown> & Traffic =>
    typeof v['upload'] === 'number' &&
    Number.isFinite(v['upload']) &&
    typeof v['download'] === 'number' &&
    Number.isFinite(v['download']);
  if (
    !total(b['total']) ||
    !total(b['wl_total']) ||
    typeof b['monthly'] !== 'number' ||
    !Number.isFinite(b['monthly']) ||
    typeof b['wl_monthly'] !== 'number' ||
    !Number.isFinite(b['wl_monthly']) ||
    typeof b['limit'] !== 'number' ||
    !Number.isFinite(b['limit']) ||
    typeof b['wl_limit'] !== 'number' ||
    !Number.isFinite(b['wl_limit']) ||
    typeof value['displayname'] !== 'string' ||
    typeof value['link'] !== 'string' ||
    typeof value['uuid'] !== 'string' ||
    typeof value['fingerprint'] !== 'string' ||
    typeof value['token'] !== 'string' ||
    typeof value['online'] !== 'boolean' ||
    typeof value['enabled'] !== 'boolean' ||
    typeof value['wl_enabled'] !== 'boolean' ||
    typeof value['time'] !== 'number' ||
    !Number.isFinite(value['time'])
  )
    return null;
  return {
    displayname: value['displayname'],
    link: value['link'],
    uuid: value['uuid'],
    fingerprint: value['fingerprint'],
    token: value['token'],
    online: value['online'],
    enabled: value['enabled'],
    wl_enabled: value['wl_enabled'],
    time: value['time'],
    bandwidth: {
      total: b['total'],
      wl_total: b['wl_total'],
      monthly: b['monthly'],
      wl_monthly: b['wl_monthly'],
      limit: b['limit'],
      wl_limit: b['wl_limit'],
    },
  };
}

export function getBwColor(pct: number): string {
  if (pct <= 25) return 'var(--green)';
  if (pct <= 50) return 'var(--yellow)';
  if (pct <= 75) return 'var(--orange)';
  if (pct <= 90) return 'var(--red)';
  return 'var(--red-dark)';
}

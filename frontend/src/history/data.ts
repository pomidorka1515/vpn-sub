import { isRecord } from '../shared/json';

export interface Sample { ts: number; up: number; down: number; wl_up: number; wl_down: number }

export function parseSamples(value: unknown): Sample[] | null {
  if (!Array.isArray(value)) return null;
  const result: Sample[] = [];
  for (const entry of value) {
    if (!isRecord(entry) || typeof entry['ts'] !== 'number' || !Number.isFinite(entry['ts']) || Math.abs(entry['ts']) > 8.64e12 ||
      typeof entry['up'] !== 'number' || !Number.isFinite(entry['up']) || typeof entry['down'] !== 'number' || !Number.isFinite(entry['down']) ||
      typeof entry['wl_up'] !== 'number' || !Number.isFinite(entry['wl_up']) || typeof entry['wl_down'] !== 'number' || !Number.isFinite(entry['wl_down'])) return null;
    result.push({ ts: entry['ts'], up: entry['up'], down: entry['down'], wl_up: entry['wl_up'], wl_down: entry['wl_down'] });
  }
  return result;
}

export function fmtDay(ts: number): string {
  return new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

export function selectSamples(entries: Sample[], days: number, now: () => number = Date.now): Sample[] {
  const cutoff = Math.floor(now() / 1000 / 86400) * 86400 - (days - 1) * 86400;
  return entries.filter(entry => entry.ts >= cutoff).sort((a, b) => a.ts - b.ts);
}

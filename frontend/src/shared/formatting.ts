export type Translate = (key: string) => string;

export function fmtBytes(bytes: number, translate: Translate): string {
  if (!Number.isFinite(bytes)) return translate('na');
  if (bytes <= 0) return '0 ' + translate('byte_unit');
  const units = ['byte_unit', 'kb_unit', 'mb_unit', 'gb_unit', 'tb_unit'];
  const index = Math.max(0, Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), units.length - 1));
  return (bytes / Math.pow(1000, index)).toFixed(index > 1 ? 2 : 0) + ' ' + translate(units[index] ?? 'byte_unit');
}

export function fmtUptime(seconds: number | null | undefined, translate: Translate): string {
  if (seconds == null || !Number.isFinite(seconds)) return translate('na');
  seconds = Math.floor(seconds);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (days > 0) return String(days) + translate('day_short') + ' ' + String(hours) + translate('hour_short') + ' ' + String(minutes) + translate('minute_short');
  if (hours > 0) return String(hours) + translate('hour_short') + ' ' + String(minutes) + translate('minute_short') + ' ' + String(rest) + translate('second_short');
  return String(minutes) + translate('minute_short') + ' ' + String(rest) + translate('second_short');
}

export function fmtTime(timestamp: number | null | undefined, translate: Translate = key => key, now: () => number = Date.now): string {
  if (!timestamp) return translate('unlimited');
  const time = timestamp * 1000;
  const current = now();
  if (time < current) return translate('expired');
  const difference = time - current;
  const days = Math.floor(difference / 86400000);
  if (days > 0) return String(days) + translate('d_left');
  return String(Math.floor(difference / 3600000)) + translate('h_left');
}

export type Translate = (key: string) => string;

export function fmtBytes(bytes: number, translate: Translate): string {
  if (!Number.isFinite(bytes)) return translate('na');
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.max(0, Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), units.length - 1));
  return (bytes / Math.pow(1000, index)).toFixed(index > 1 ? 2 : 0) + ' ' + units[index];
}

export function fmtUptime(seconds: number | null | undefined, translate: Translate): string {
  if (seconds == null || !Number.isFinite(seconds)) return translate('na');
  seconds = Math.floor(seconds);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (days > 0) return days + 'd ' + hours + 'h ' + minutes + 'm';
  if (hours > 0) return hours + 'h ' + minutes + 'm ' + rest + 's';
  return minutes + 'm ' + rest + 's';
}

export function fmtTime(timestamp: number | null | undefined, translate: Translate = key => key, now: () => number = Date.now): string {
  if (!timestamp) return translate('unlimited');
  const time = timestamp * 1000;
  const current = now();
  if (time < current) return translate('expired');
  const difference = time - current;
  const days = Math.floor(difference / 86400000);
  if (days > 0) return days + translate('d_left');
  return Math.floor(difference / 3600000) + translate('h_left');
}

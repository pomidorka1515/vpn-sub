// Floor Y ranges so a chart starts large and only grows. Chart.js otherwise
// refits every sample, which makes a quiet series fill the card and jump.

const BYTE = 1024;
const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

export function defaultMax(unit) {
  if (unit === 'percent') return 100;
  if (unit === 'count') return 100;
  if (unit === 'float') return 4;
  if (unit === 'seconds') return DAY;
  if (unit === 'bytes') return 16 * BYTE * BYTE * BYTE;
  if (unit === 'bytes_per_s') return 8 * BYTE * BYTE;
  return 100;
}

function niceCeil(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const frac = value / base;
  const step = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  return step * base;
}

function byteCeil(value) {
  let step = BYTE;
  while (step < value) step *= 2;
  return step;
}

function secondCeil(value) {
  const steps = [MINUTE, 5 * MINUTE, 15 * MINUTE, HOUR, 6 * HOUR, 12 * HOUR, DAY, 7 * DAY, 30 * DAY];
  for (const step of steps) {
    if (value <= step) return step;
  }
  return niceCeil(value);
}

export function raisedMax(unit, peak) {
  const floor = defaultMax(unit);
  if (!Number.isFinite(peak) || peak <= floor) return floor;
  const padded = peak * 1.25;
  if (unit === 'bytes' || unit === 'bytes_per_s') return byteCeil(padded);
  if (unit === 'seconds') return secondCeil(padded);
  if (unit === 'count') return Math.max(floor, Math.ceil(padded / 10) * 10);
  return Math.max(floor, niceCeil(padded));
}

export function axisMax(unit, peak, held) {
  const next = raisedMax(unit, peak);
  return Math.max(next, Number.isFinite(held) ? held : 0);
}

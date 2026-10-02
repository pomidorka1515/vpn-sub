// Floor Y ranges so a quiet series does not fill the card. Below the ceiling
// the held max only grows. Live mode slides inside that ceiling instead of
// pinning a spike to the top of the plot.

const BYTE = 1024;
const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;
const YEAR = 365 * DAY;

export function defaultMax(unit) {
  if (unit === 'percent') return 100;
  if (unit === 'count') return 200;
  if (unit === 'float') return 8;
  if (unit === 'seconds') return 7 * DAY;
  if (unit === 'bytes') return 64 * BYTE * BYTE * BYTE;
  if (unit === 'bytes_per_s') return 32 * BYTE * BYTE;
  return 100;
}

export function ceilingMax(unit) {
  if (unit === 'percent') return 100;
  if (unit === 'count') return 10000;
  if (unit === 'float') return 64;
  if (unit === 'seconds') return YEAR;
  if (unit === 'bytes') return 16 * BYTE * BYTE * BYTE * BYTE;
  if (unit === 'bytes_per_s') return BYTE * BYTE * BYTE;
  return 10000;
}

function niceCeil(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const frac = value / base;
  const step = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  return step * base;
}

function niceFloor(value) {
  if (!Number.isFinite(value) || value <= 0) return 0;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const frac = value / base;
  const step = frac < 2 ? 1 : frac < 5 ? 2 : frac < 10 ? 5 : 10;
  const stepped = step * base;
  return stepped > value ? Math.max(0, stepped - base) : stepped;
}

function byteCeil(value) {
  let step = BYTE;
  while (step < value) step *= 2;
  return step;
}

function byteFloor(value) {
  if (!Number.isFinite(value) || value < BYTE) return 0;
  let step = BYTE;
  while (step * 2 <= value) step *= 2;
  return step;
}

function secondCeil(value) {
  const steps = [MINUTE, 5 * MINUTE, 15 * MINUTE, HOUR, 6 * HOUR, 12 * HOUR, DAY, 7 * DAY, 30 * DAY, YEAR];
  for (const step of steps) {
    if (value <= step) return step;
  }
  return niceCeil(value);
}

function secondFloor(value) {
  const steps = [YEAR, 30 * DAY, 7 * DAY, DAY, 12 * HOUR, 6 * HOUR, HOUR, 15 * MINUTE, 5 * MINUTE, MINUTE];
  for (const step of steps) {
    if (value >= step) return step;
  }
  return 0;
}

function roundCeil(unit, value) {
  if (unit === 'bytes' || unit === 'bytes_per_s') return byteCeil(value);
  if (unit === 'seconds') return secondCeil(value);
  if (unit === 'count') return Math.ceil(value / 10) * 10;
  return niceCeil(value);
}

function roundFloor(unit, value) {
  if (unit === 'bytes' || unit === 'bytes_per_s') return byteFloor(value);
  if (unit === 'seconds') return secondFloor(value);
  if (unit === 'count') return Math.floor(value / 10) * 10;
  return niceFloor(value);
}

function cap(unit, value) {
  return Math.min(value, ceilingMax(unit));
}

export function raisedMax(unit, peak) {
  const floor = defaultMax(unit);
  if (!Number.isFinite(peak) || peak <= floor) return floor;
  const padded = peak * 1.25;
  return cap(unit, Math.max(floor, roundCeil(unit, padded)));
}

export function axisMax(unit, peak, held) {
  const next = raisedMax(unit, peak);
  return Math.max(next, Number.isFinite(held) ? held : 0);
}

// Live range. Percent never slides. Below the ceiling the held max only grows.
// Past it, gauges track the visible peak and counters get a windowed baseline
// so a monotonically increasing series does not pin itself to the lid.
export function windowedRange(unit, visibleMin, visibleMax, held, live) {
  const floor = defaultMax(unit);
  const ceiling = ceilingMax(unit);
  if (unit === 'percent' || !live) {
    const max = live ? raisedMax(unit, visibleMax) : axisMax(unit, visibleMax, held);
    return { min: 0, max: unit === 'percent' ? 100 : max };
  }
  const peak = Number.isFinite(visibleMax) ? visibleMax : 0;
  const low = Number.isFinite(visibleMin) ? visibleMin : 0;
  const rounded = raisedMax(unit, peak);
  if (rounded < ceiling && !(Number.isFinite(held) && held >= ceiling)) {
    return { min: 0, max: Math.max(rounded, Number.isFinite(held) ? held : 0) };
  }
  let max = cap(unit, Math.max(floor, roundCeil(unit, peak * 1.25)));
  let min = 0;
  // A counter that only climbs sits in the top quarter once it clears the
  // floor. Slide the baseline so the stroke keeps moving through the card.
  // A gauge that still reaches toward zero keeps y.min at 0.
  if (low > floor && peak > 0 && (peak - low) <= peak * 0.25) {
    min = Math.max(0, roundFloor(unit, low * 0.9));
    max = cap(unit, Math.max(min + floor, roundCeil(unit, peak * 1.1)));
    if (max <= min) max = cap(unit, min + floor);
  }
  return { min, max };
}

// Missing paths and non-numeric values must not be plotted as zero.
export interface SeriesSpec {
  path: readonly string[];
  scale?: string;
  totalPath?: readonly string[];
  altTotalPath?: readonly string[];
  altPath?: readonly string[];
}

export function readPath(obj: unknown, path: readonly string[] | undefined): unknown {
  if (!path?.length) return null;
  let current = obj;
  for (const key of path) {
    if (current === null || typeof current !== 'object') return null;
    current = Reflect.get(current, key) as unknown;
  }
  return current;
}

function asNumber(value: unknown): number | null {
  if (value == null || value === '') return null;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

export function readSeries(obj: unknown, spec: SeriesSpec): number | null {
  if (obj == null) return null;
  if (spec.scale === 'percent') {
    const used = asNumber(readPath(obj, spec.path));
    let rawTotal = readPath(obj, spec.totalPath);
    if (rawTotal == null && spec.altTotalPath) rawTotal = readPath(obj, spec.altTotalPath);
    const total = asNumber(rawTotal);
    if (used == null || total == null || total <= 0) return null;
    return used / total * 100;
  }
  let value = readPath(obj, spec.path);
  if (value == null && spec.altPath) value = readPath(obj, spec.altPath);
  return asNumber(value);
}

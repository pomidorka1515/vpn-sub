// Pull one numeric point out of a host or panel object.
// Missing path yields null. Never coerce a missing value to 0.

export function readPath(obj, path) {
  if (!path || !path.length) return null;
  let cur = obj;
  for (const key of path) {
    if (cur == null) return null;
    if (typeof cur !== 'object') return null;
    cur = cur[key];
  }
  return cur;
}

function asNumber(value) {
  if (value == null || value === '') return null;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function readSeries(obj, spec) {
  if (obj == null) return null;
  if (spec.scale === 'percent') {
    const used = asNumber(readPath(obj, spec.path));
    let total = readPath(obj, spec.totalPath);
    if (total == null && spec.altTotalPath) total = readPath(obj, spec.altTotalPath);
    total = asNumber(total);
    if (used == null || total == null || total <= 0) return null;
    return used / total * 100;
  }
  let value = readPath(obj, spec.path);
  if (value == null && spec.altPath) value = readPath(obj, spec.altPath);
  return asNumber(value);
}

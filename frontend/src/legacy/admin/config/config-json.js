// Preserve Python's integer/float distinction and integers beyond JS's safe range.
export class ConfigNumber {
  constructor(raw) { this.raw = raw; }
  toString() { return this.raw; }
}

const configKeyOrder = new WeakMap();

export function parseConfigJson(text) {
  let pos = 0;
  const whitespace = () => { while (pos < text.length && /[\x20\t\r\n]/.test(text[pos])) pos++; };
  const value = () => {
    whitespace();
    const start = pos;
    if (text[pos] === '"') {
      pos++;
      while (pos < text.length) {
        if (text.charCodeAt(pos) < 32) throw new SyntaxError('Invalid JSON string');
        if (text[pos] === '\\') {
          pos++;
          if (text[pos] === 'u') {
            pos++;
            for (let digit = 0; digit < 4; digit++, pos++) {
              if (!/[0-9a-fA-F]/.test(text[pos] || '')) throw new SyntaxError('Invalid JSON escape');
            }
          } else {
            if (!['"', '\\', '/', 'b', 'f', 'n', 'r', 't'].includes(text[pos])) throw new SyntaxError('Invalid JSON escape');
            pos++;
          }
        } else if (text[pos++] === '"') return JSON.parse(text.slice(start, pos));
      }
      throw new SyntaxError('Invalid JSON');
    }
    if (text[pos] === '{' || text[pos] === '[') {
      const object = text[pos++] === '{';
      const result = object ? Object.create(null) : [];
      if (object) configKeyOrder.set(result, []);
      const end = object ? '}' : ']';
      whitespace();
      if (text[pos] === end) { pos++; return result; }
      while (true) {
        if (object) {
          whitespace();
          if (text[pos] !== '"') throw new SyntaxError('Invalid JSON');
          const key = value();
          whitespace();
          if (text[pos] !== ':') throw new SyntaxError('Invalid JSON');
          pos++;
          if (!Object.hasOwn(result, key)) configKeyOrder.get(result).push(key);
          result[key] = value();
        } else result.push(value());
        whitespace();
        if (text[pos] === end) { pos++; return result; }
        if (text[pos] !== ',') throw new SyntaxError('Invalid JSON');
        pos++;
      }
    }
    for (const [token, result] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(token, pos)) { pos += token.length; return result; }
    }
    const number = text.slice(pos).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (!number) throw new SyntaxError('Invalid JSON');
    if (/[.eE]/.test(number[0]) && !Number.isFinite(Number(number[0]))) throw new SyntaxError('Non-finite JSON number');
    pos += number[0].length;
    return new ConfigNumber(number[0]);
  };
  try {
    const result = value();
    whitespace();
    if (pos !== text.length) throw new SyntaxError('Invalid JSON');
    return result;
  } catch (error) {
    error.position = Math.min(pos, text.length);
    throw error;
  }
}

export function stringifyConfig(value, pretty = false, depth = 0) {
  if (value instanceof ConfigNumber) return value.raw;
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  const array = Array.isArray(value);
  const entries = array ? value.map(item => stringifyConfig(item, pretty, depth + 1))
    : [...new Set([...(configKeyOrder.get(value) || []), ...Object.keys(value)])]
      .filter(key => Object.hasOwn(value, key))
      .map(key => JSON.stringify(key) + (pretty ? ': ' : ':') + stringifyConfig(value[key], pretty, depth + 1));
  const [open, close] = array ? ['[', ']'] : ['{', '}'];
  if (!pretty || !entries.length) return open + entries.join(',') + close;
  return open + '\n' + entries.map(item => '  '.repeat(depth + 1) + item).join(',\n') + '\n' + '  '.repeat(depth) + close;
}

export function clone(value) {
  return value === undefined ? undefined : parseConfigJson(stringifyConfig(value));
}

export function jsonEqual(left, right) {
  if (left instanceof ConfigNumber || right instanceof ConfigNumber) {
    if (!(left instanceof ConfigNumber) || !(right instanceof ConfigNumber)) return false;
    const integer = raw => !/[.eE]/.test(raw);
    if (integer(left.raw) !== integer(right.raw)) return false;
    return integer(left.raw) ? BigInt(left.raw) === BigInt(right.raw) : Number(left.raw) === Number(right.raw);
  }
  if (left === undefined && right === undefined) return true;
  if (left === undefined || right === undefined) return false;
  if (Object.prototype.toString.call(left) !== Object.prototype.toString.call(right)) return false;
  if (left && typeof left === 'object') {
    if (Array.isArray(left)) {
      if (left.length !== right.length) return false;
      return left.every((item, i) => jsonEqual(item, right[i]));
    }
    const keys = Object.keys(left);
    if (keys.length !== Object.keys(right).length) return false;
    return keys.every(key => Object.prototype.hasOwnProperty.call(right, key) && jsonEqual(left[key], right[key]));
  }
  return left === right;
}

export function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && !(value instanceof ConfigNumber);
}

export function isInteger(value) {
  return value instanceof ConfigNumber ? !/[.eE]/.test(value.raw) : typeof value === 'number' && Number.isInteger(value);
}

export function isFiniteNumber(value) {
  return value instanceof ConfigNumber ? Number.isFinite(Number(value.raw)) : typeof value === 'number' && Number.isFinite(value);
}

export function jsonText(value) {
  try { return stringifyConfig(value ?? {}, true); } catch { return ''; }
}

export function parseJsonField(raw, fallback) {
  if (raw == null) return fallback;
  try { return parseConfigJson(raw); } catch { return raw; }
}

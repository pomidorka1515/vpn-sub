import { sharedMessage } from '../../shared/messages';

// Preserve Python's integer/float distinction and integers beyond JS's safe range.
export class ConfigNumber {
  constructor(readonly raw: string) {}
  toString(): string { return this.raw; }
}

export type ConfigValue = null | boolean | string | number | ConfigNumber | ConfigValue[] | ConfigObject;
export interface ConfigObject { [key: string]: ConfigValue }

const keyOrder = new WeakMap<ConfigObject, string[]>();

export function parseConfigJson(text: string): ConfigValue {
  let pos = 0;
  const whitespace = (): void => { while (pos < text.length && /[\x20\t\r\n]/.test(text.charAt(pos))) pos++; };
  const value = (): ConfigValue => {
    whitespace();
    const start = pos;
    if (text[pos] === '"') {
      pos++;
      while (pos < text.length) {
        if (text.charCodeAt(pos) < 32) throw new SyntaxError(sharedMessage('json_string'));
        if (text[pos] === '\\') {
          pos++;
          if (text[pos] === 'u') {
            pos++;
            for (let digit = 0; digit < 4; digit++, pos++) {
              if (!/[0-9a-fA-F]/.test(text.charAt(pos))) throw new SyntaxError(sharedMessage('json_escape'));
            }
          } else {
            if (!['"', '\\', '/', 'b', 'f', 'n', 'r', 't'].includes(text.charAt(pos))) throw new SyntaxError(sharedMessage('json_escape'));
            pos++;
          }
        } else if (text[pos++] === '"') {
          const parsed: unknown = JSON.parse(text.slice(start, pos));
          if (typeof parsed !== 'string') throw new SyntaxError(sharedMessage('json_string'));
          return parsed;
        }
      }
      throw new SyntaxError(sharedMessage('json_invalid'));
    }
    if (text[pos] === '{') {
      pos++;
      const result: ConfigObject = {};
      Object.setPrototypeOf(result, null);
      const keys: string[] = [];
      keyOrder.set(result, keys);
      whitespace();
      if (text[pos] === '}') { pos++; return result; }
      for (;;) {
        whitespace();
        if (text[pos] !== '"') throw new SyntaxError(sharedMessage('json_invalid'));
        const key = value();
        if (typeof key !== 'string') throw new SyntaxError(sharedMessage('json_invalid'));
        whitespace();
        if (text[pos] !== ':') throw new SyntaxError(sharedMessage('json_invalid'));
        pos++;
        if (!Object.hasOwn(result, key)) keys.push(key);
        result[key] = value();
        whitespace();
        if (text[pos] === '}') { pos++; return result; }
        if (text[pos] !== ',') throw new SyntaxError(sharedMessage('json_invalid'));
        pos++;
      }
    }
    if (text[pos] === '[') {
      pos++;
      const result: ConfigValue[] = [];
      whitespace();
      if (text[pos] === ']') { pos++; return result; }
      for (;;) {
        result.push(value());
        whitespace();
        if (text[pos] === ']') { pos++; return result; }
        if (text[pos] !== ',') throw new SyntaxError(sharedMessage('json_invalid'));
        pos++;
      }
    }
    for (const [token, result] of [['true', true], ['false', false], ['null', null]] as const) {
      if (text.startsWith(token, pos)) { pos += token.length; return result; }
    }
    const number = text.slice(pos).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/)?.[0];
    if (!number) throw new SyntaxError(sharedMessage('json_invalid'));
    if (/[.eE]/.test(number) && !Number.isFinite(Number(number))) throw new SyntaxError(sharedMessage('json_number'));
    pos += number.length;
    return new ConfigNumber(number);
  };
  try {
    const result = value();
    whitespace();
    if (pos !== text.length) throw new SyntaxError(sharedMessage('json_invalid'));
    return result;
  } catch (error) {
    if (error instanceof Error) Object.assign(error, { position: Math.min(pos, text.length) });
    throw error;
  }
}

export function stringifyConfig(value: ConfigValue, pretty = false, depth = 0): string {
  if (value instanceof ConfigNumber) return value.raw;
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  const array = Array.isArray(value);
  const entries = array ? value.map(item => stringifyConfig(item, pretty, depth + 1))
    : [...new Set([...(keyOrder.get(value) || []), ...Object.keys(value)])]
      .filter(key => Object.hasOwn(value, key))
      .map(key => JSON.stringify(key) + (pretty ? ': ' : ':') + stringifyConfig(value[key] ?? null, pretty, depth + 1));
  const [open, close] = array ? ['[', ']'] : ['{', '}'];
  if (!pretty || !entries.length) return open + entries.join(',') + close;
  return open + '\n' + entries.map(item => '  '.repeat(depth + 1) + item).join(',\n') + '\n' + '  '.repeat(depth) + close;
}

export function clone(value: ConfigObject): ConfigObject;
export function clone(value: ConfigObject | null): ConfigObject | null;
export function clone(value: undefined): undefined;
export function clone(value: ConfigValue): ConfigValue;
export function clone(value: ConfigValue | undefined): ConfigValue | undefined;
export function clone(value: ConfigValue | undefined): ConfigValue | undefined {
  if (value === undefined) return undefined;
  const result = parseConfigJson(stringifyConfig(value));
  if (isPlainObject(value) && !isPlainObject(result)) throw new Error(sharedMessage('json_invalid'));
  return result;
}

export function jsonEqual(left: ConfigValue | undefined, right: ConfigValue | undefined): boolean {
  if (left instanceof ConfigNumber || right instanceof ConfigNumber) {
    if (!(left instanceof ConfigNumber) || !(right instanceof ConfigNumber)) return false;
    const integer = (raw: string): boolean => !/[.eE]/.test(raw);
    if (integer(left.raw) !== integer(right.raw)) return false;
    return integer(left.raw) ? BigInt(left.raw) === BigInt(right.raw) : Number(left.raw) === Number(right.raw);
  }
  if (left === undefined || right === undefined) return left === right;
  if (Object.prototype.toString.call(left) !== Object.prototype.toString.call(right)) return false;
  if (Array.isArray(left)) {
    return Array.isArray(right) && left.length === right.length && left.every((item, i) => jsonEqual(item, right[i]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && jsonEqual(left[key], right[key]));
  }
  return left === right;
}

export function isPlainObject(value: unknown): value is ConfigObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && !(value instanceof ConfigNumber);
}

export function isInteger(value: unknown): boolean {
  return value instanceof ConfigNumber ? !/[.eE]/.test(value.raw) : typeof value === 'number' && Number.isInteger(value);
}

export function isFiniteNumber(value: unknown): boolean {
  return value instanceof ConfigNumber ? Number.isFinite(Number(value.raw)) : typeof value === 'number' && Number.isFinite(value);
}

export function jsonText(value: ConfigValue | undefined): string {
  try { return stringifyConfig(value ?? {}, true); } catch { return ''; }
}

export function parseJsonField(raw: string | null | undefined, fallback: ConfigValue): ConfigValue {
  if (raw == null) return fallback;
  try { return parseConfigJson(raw); } catch { return raw; }
}

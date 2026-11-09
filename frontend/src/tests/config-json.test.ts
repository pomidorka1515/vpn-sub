import { describe, expect, it } from 'vitest';
import { clone, ConfigNumber, isFiniteNumber, isInteger, jsonEqual, parseConfigJson, parseJsonField, stringifyConfig } from '../admin/config/json';

describe('lossless config JSON', () => {
  it('round trips numeric spelling, integer-like keys, escapes and dangerous property names', () => {
    const raw = '{"10":900719925474099312345,"2":1.00,"__proto__":{"x":-0},"text":"a\\n\\u263a","a":[true,null,1e-3]}';
    const parsed = parseConfigJson(raw);
    expect(stringifyConfig(parsed)).toBe('{"10":900719925474099312345,"2":1.00,"__proto__":{"x":-0},"text":"a\\n☺","a":[true,null,1e-3]}');
    expect(JSON.parse(stringifyConfig(parsed, true))).toEqual(JSON.parse(raw));
    expect(({} as { x?: number }).x).toBeUndefined();
    expect(jsonEqual(parsed, clone(parsed))).toBe(true);
    expect(clone(parsed)).not.toBe(parsed);
  });

  it('keeps the last duplicate value while retaining its original key position', () => {
    expect(stringifyConfig(parseConfigJson('{"b":1,"a":2,"b":3}'))).toBe('{"b":3,"a":2}');
  });

  it.each(['', '01', '-01', '1.', '.1', '1e', '1e9999', 'true false', '[1,]', '{"a":1,}', '{a:1}', '"\\u12xz"', '"\\q"', '"line\nfeed"', '"unterminated', '{"a" 1}'])('rejects invalid JSON %j with an error position', raw => {
    try { parseConfigJson(raw); throw new Error('expected a syntax error'); }
    catch (error) {
      expect(error).toBeInstanceOf(SyntaxError);
      if (!(error instanceof SyntaxError)) throw error;
      const position: unknown = Reflect.get(error, 'position');
      if (typeof position !== 'number') throw new Error('missing error position');
      expect(position).toBeGreaterThanOrEqual(0);
      expect(position).toBeLessThanOrEqual(raw.length);
    }
  });

  it('compares numeric value without losing integer versus float type', () => {
    expect(jsonEqual(new ConfigNumber('9007199254740993'), new ConfigNumber('9007199254740992'))).toBe(false);
    expect(jsonEqual(new ConfigNumber('-0'), new ConfigNumber('0'))).toBe(true);
    expect(jsonEqual(new ConfigNumber('1.0'), new ConfigNumber('1e0'))).toBe(true);
    expect(jsonEqual(new ConfigNumber('1'), new ConfigNumber('1.0'))).toBe(false);
    expect(jsonEqual(parseConfigJson('{"a":[1],"b":true}'), parseConfigJson('{"b":true,"a":[1]}'))).toBe(true);
    expect(jsonEqual(parseConfigJson('[1,2]'), parseConfigJson('[2,1]'))).toBe(false);
    expect(jsonEqual(undefined, null)).toBe(false);
  });

  it('distinguishes integer, finite, invalid field text and missing fields', () => {
    expect(isInteger(new ConfigNumber('1e0'))).toBe(false);
    expect(isInteger(new ConfigNumber('999999999999999999999'))).toBe(true);
    expect(isFiniteNumber(new ConfigNumber('1e999'))).toBe(false);
    expect(parseJsonField(null, false)).toBe(false);
    expect(parseJsonField('{broken', false)).toBe('{broken');
    expect(stringifyConfig(parseJsonField('[1]', false))).toBe('[1]');
  });
});

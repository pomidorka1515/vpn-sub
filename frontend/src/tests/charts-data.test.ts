import { describe, expect, it } from 'vitest';
import { axisMax, ceilingMax, defaultMax, raisedMax, windowedRange } from '../admin/charts/scale';
import { readPath, readSeries } from '../admin/charts/series';
import { getBwColor, parseStats } from '../dashboard/data';
import { fmtDay, parseSamples, selectSamples } from '../history/data';

describe('chart series', () => {
  it('reads nested paths and arrays without turning absent data into zero', () => {
    expect(readPath({ a: [{ b: 0 }] }, ['a', 0, 'b'])).toBe(0);
    expect(readPath({ a: null }, ['a', 'b'])).toBeNull();
    expect(readPath({}, [])).toBeNull();
    expect(readSeries({ a: 0, b: 10 }, { path: ['a'], altPath: ['b'] })).toBe(0);
    expect(readSeries({ b: '12.5' }, { path: ['a'], altPath: ['b'] })).toBe(12.5);
    for (const value of ['', null, 'bad', Infinity, NaN]) expect(readSeries({ a: value }, { path: ['a'] })).toBeNull();
  });

  it('calculates percentages only with a positive finite denominator', () => {
    const spec = { path: ['used'], totalPath: ['total'], altTotalPath: ['capacity'], scale: 'percent' };
    expect(readSeries({ used: '25', capacity: 100 }, spec)).toBe(25);
    expect(readSeries({ used: 0, total: 100 }, spec)).toBe(0);
    expect(readSeries({ used: 150, total: 100 }, spec)).toBe(150);
    for (const total of [0, -1, '', Infinity, 'bad']) expect(readSeries({ used: 5, total, capacity: 100 }, spec)).toBeNull();
  });
});

describe('chart axes', () => {
  it.each(['percent', 'count', 'float', 'seconds', 'bytes', 'bytes_per_s', 'unknown'])('holds the floor and caps a raised %s axis', unit => {
    expect(raisedMax(unit, NaN)).toBe(defaultMax(unit));
    expect(raisedMax(unit, defaultMax(unit))).toBe(defaultMax(unit));
    expect(raisedMax(unit, ceilingMax(unit) * 10)).toBe(ceilingMax(unit));
    expect(axisMax(unit, 0, defaultMax(unit) * 2)).toBe(defaultMax(unit) * 2);
  });

  it('slides a live counter baseline at the ceiling but not a gauge or snapshot', () => {
    expect(windowedRange('count', 9500, 10000, 10000, true)).toEqual({ min: 8550, max: 10000 });
    expect(windowedRange('count', 0, 10000, 10000, true)).toEqual({ min: 0, max: 10000 });
    expect(windowedRange('count', 9500, 10000, 12000, false)).toEqual({ min: 0, max: 12000 });
    expect(windowedRange('percent', 90, 150, 200, true)).toEqual({ min: 0, max: 100 });
    expect(windowedRange('float', NaN, NaN, NaN, true)).toEqual({ min: 0, max: 4 });
  });
});

describe('history samples', () => {
  const sample = { ts: 86400, up: 1, down: 2, wl_up: 3, wl_down: 4 };
  it('validates every sample and strips unrelated fields', () => {
    expect(parseSamples([{ ...sample, extra: 'secret' }])).toEqual([sample]);
    expect(parseSamples([])).toEqual([]);
    for (const key of Object.keys(sample)) {
      for (const bad of [undefined, '1', NaN, Infinity]) expect(parseSamples([{ ...sample, [key]: bad }])).toBeNull();
    }
    expect(parseSamples([sample, null])).toBeNull();
    expect(parseSamples([{ ...sample, ts: 8.64e12 + 1 }])).toBeNull();
    expect(parseSamples({})).toBeNull();
  });
  it('filters at UTC midnight inclusively and sorts without mutating input', () => {
    const rows = [3, 1, 2, 0].map(day => ({ ...sample, ts: day * 86400 }));
    expect(selectSamples(rows, 2, () => 3.5 * 86400 * 1000).map(row => row.ts)).toEqual([2 * 86400, 3 * 86400]);
    expect(rows.map(row => row.ts)).toEqual([3 * 86400, 86400, 2 * 86400, 0]);
    expect(fmtDay(0)).toBe('1970-01-01 00:00:00 UTC');
  });
});

describe('dashboard stats', () => {
  const stats = { displayname: 'Alice', link: '', uuid: 'u', fingerprint: 'f', token: 't', online: false, enabled: true, wl_enabled: false, time: 0,
    bandwidth: { total: { upload: 0, download: 1 }, wl_total: { upload: 2, download: 3 }, monthly: 0, wl_monthly: 0, limit: 0, wl_limit: 0 } };
  it('requires complete, finite data even when the rest is valid', () => {
    expect(parseStats(stats)).toEqual(stats);
    for (const key of ['monthly', 'wl_monthly', 'limit', 'wl_limit']) expect(parseStats({ ...stats, bandwidth: { ...stats.bandwidth, [key]: Infinity } })).toBeNull();
    expect(parseStats({ ...stats, time: NaN })).toBeNull();
    expect(parseStats({ ...stats, online: 1 })).toBeNull();
    expect(parseStats({ ...stats, bandwidth: { ...stats.bandwidth, total: { upload: 0, download: '0' } } })).toBeNull();
  });
  it.each([[25, 'green'], [25.1, 'yellow'], [50, 'yellow'], [50.1, 'orange'], [75, 'orange'], [75.1, 'red'], [90, 'red'], [90.1, 'red-dark']])('colors %s%% at threshold', (pct, color) => {
    expect(getBwColor(pct)).toBe(`var(--${color})`);
  });
});

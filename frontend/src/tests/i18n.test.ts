import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApi } from '../shared/api';
import type { ApiResult } from '../shared/api';
import { isRecord, stringTable } from '../shared/json';
import { fmtBytes, fmtUptime } from '../shared/formatting';
import { serverMessage, sharedMessage } from '../shared/messages';
import { initError } from '../pages/error';
import { initRedirect } from '../pages/redirect';
import { MAIN_SECTIONS } from '../admin/config/state';

const parsed = ts.parseConfigFileTextToJson('lang.jsonc', readFileSync('../lang.jsonc', 'utf8'));
const config: unknown = parsed.config;
if (!isRecord(config) || !isRecord(config['web'])) throw new Error('invalid language configuration');
const web: Record<string, Record<'en' | 'ru', Record<string, string>>> = {};
for (const [name, section] of Object.entries(config['web'])) {
  if (!isRecord(section)) throw new Error('invalid language section');
  web[name] = { en: stringTable(section['en']), ru: stringTable(section['ru']) };
}
const originalLanguage = document.documentElement.lang;
afterEach(() => { document.documentElement.lang = originalLanguage; localStorage.clear(); document.body.innerHTML = ''; });

function files(path: string): string[] {
  return readdirSync(path, { withFileTypes: true }).flatMap(entry => entry.isDirectory() ? files(join(path, entry.name)) : [join(path, entry.name)]);
}

describe('frontend translations', () => {
  it('parses the language file and keeps English and Russian keys in sync', () => {
    expect(parsed.error).toBeUndefined();
    for (const section of Object.values(web)) {
      expect(Object.keys(section.ru).sort()).toEqual(Object.keys(section.en).sort());
      for (const table of Object.values(section)) for (const value of Object.values(table)) expect(value.trim()).not.toBe('');
    }
  });

  it('defines every literal translation key and every main config label', () => {
    const keys = new Set(Object.values(web).flatMap(section => Object.keys(section.en)));
    for (const path of files('src').filter(path => !path.includes('/tests/') && /\.(ts|html)$/.test(path))) {
      const source = readFileSync(path, 'utf8');
      for (const match of source.matchAll(/(?:\bt|\bsharedMessage)\('([\w]+)'\)|data-t(?:-aria|-title|-ph)?="([\w]+)"/g)) {
        expect(keys.has(match[1] ?? match[2] ?? ''), path + ': ' + match[0]).toBe(true);
      }
    }
    for (const [, fields] of MAIN_SECTIONS) for (const field of fields) expect(web['admin']?.en['config_field_' + field.replace('$', '')]).toBeTruthy();
  });

  it('does not hardcode exception messages in application source', () => {
    for (const path of files('src').filter(path => !path.includes('/tests/') && path.endsWith('.ts'))) {
      const source = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true);
      function check(node: ts.Node): void {
        if (ts.isNewExpression(node) && ['Error', 'SyntaxError'].includes(node.expression.getText(source))) {
          const message = node.arguments?.[0];
          expect(message && (ts.isStringLiteral(message) || ts.isTemplateExpression(message)), path).toBeFalsy();
        }
        ts.forEachChild(node, check);
      }
      check(source);
    }
  });

  it.each(['en', 'ru'] as const)('localizes units and diagnostics in %s', language => {
    const t = (key: string) => sharedMessage(key, {}, language);
    expect(fmtBytes(0, t)).toBe('0 ' + t('byte_unit'));
    expect(fmtBytes(1e9, t)).toBe('1.00 ' + t('gb_unit'));
    expect(fmtUptime(90061, t)).toBe('1' + t('day_short') + ' 1' + t('hour_short') + ' 1' + t('minute_short'));
    expect(sharedMessage('missing_element', { id: 'test' }, language)).toContain('#test');
    expect(serverMessage('Invalid credentials.', language)).toBe(web['errors']?.[language]['Invalid credentials']);
    expect(serverMessage('Schema validation error: secret', language)).toBe(web['errors']?.[language]['Schema validation error']);
    expect(serverMessage('unrecognized private error', language)).toBe(web['errors']?.[language]['Request failed']);
    expect(serverMessage('Missing fields: username, password', language)).toBe(web['errors']?.[language]['Missing fields: {fields}']?.replace('{fields}', 'username, password'));
  });

  it('localizes API failures without changing authentication decisions or raw messages', async () => {
    document.documentElement.lang = 'ru';
    const on401 = vi.fn();
    const allow401 = vi.fn((result: ApiResult) => result.msg === 'Invalid current password');
    const request = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ success: false, msg: 'Invalid current password' }, { status: 401 }));
    const api = createApi(() => ({ on401 }), sharedMessage, vi.fn(), request);
    const result = await api('POST', '/settings', {}, { allow401 });
    expect(on401).not.toHaveBeenCalled();
    expect(allow401).toHaveBeenCalledOnce();
    expect(result?.msg).toBe(web['errors']?.ru['Invalid current password']);
    expect(result?.rawMsg).toBe('Invalid current password');
  });

  it('does not display the browser HTTP status text for malformed responses', async () => {
    const api = createApi(() => ({}), sharedMessage, vi.fn(), vi.fn<typeof fetch>().mockResolvedValue(new Response('<html>', { status: 502, statusText: 'Bad Gateway' })));
    expect((await api('GET', '/stats'))?.msg).toBe(sharedMessage('invalid_response'));
  });

  it.each(['en', 'ru'] as const)('renders standalone pages in %s', language => {
    localStorage.setItem('lang', language);
    document.body.innerHTML = '<h1 id="heading"></h1><p id="description"></p><button id="back"><span id="backLabel"></span></button>';
    const dispose = initError(document, { length: 1, back: vi.fn() }, navigator);
    document.dispatchEvent(new Event('DOMContentLoaded'));
    expect(document.title).toBe(sharedMessage('not_found_title', {}, language));
    expect(document.getElementById('heading')?.textContent).toBe(sharedMessage('not_found_heading', {}, language));
    dispose();
    document.body.innerHTML = '<span id="redirect-label"></span><a id="manual-link"></a>';
    const replace = vi.fn();
    initRedirect(document, { search: '?lang=' + language + '&url=https%3A%2F%2Fexample.test', replace });
    expect(document.getElementById('manual-link')?.textContent).toBe(sharedMessage('redirect_link', {}, language));
    expect(replace).toHaveBeenCalledWith('https://example.test');
  });
});

import type { ApiResult } from '../../shared/api';
import { clone, isPlainObject, jsonEqual, parseConfigJson, stringifyConfig } from './json';
import type { ConfigObject } from './json';

type Request = (method: string, path: string, body?: unknown) => Promise<ApiResult | null>;
export interface ConfigState {
  loaded: ConfigObject | null;
  draft: ConfigObject | null;
  etag: string;
  conflict: boolean;
  loading: boolean;
  saving: boolean;
}
export type SaveResult =
  | { kind: 'busy' | 'unchanged' | 'network' | 'conflict' | 'reauth' }
  | { kind: 'error'; message: string }
  | { kind: 'saved'; message: string; restart: string[] };

export function createConfigService(api: Request) {
  const state: ConfigState = { loaded: null, draft: null, etag: '', conflict: false, loading: false, saving: false };
  function dirty(): boolean { return state.loaded !== null && state.draft !== null && !jsonEqual(state.loaded, state.draft); }
  async function load(): Promise<'ok' | 'busy' | 'error'> {
    if (state.loading || state.saving) return 'busy';
    state.loading = true;
    try {
      const res = await api('GET', '/api/config/get');
      if (!res?.success || !res.etag || !isPlainObject(res.obj)) return 'error';
      const parsed = res.rawJson ? parseConfigJson(res.rawJson) : null;
      const config = parsed && isPlainObject(parsed) ? parsed.obj : res.obj;
      if (!isPlainObject(config)) return 'error';
      state.loaded = clone(config);
      state.draft = clone(config);
      state.etag = res.etag;
      state.conflict = false;
      return 'ok';
    } catch { return 'error'; }
    finally { state.loading = false; }
  }
  async function save(): Promise<SaveResult> {
    if (state.loading || state.saving || !state.loaded || !state.draft) return { kind: 'busy' };
    if (state.conflict) return { kind: 'conflict' };
    const values: ConfigObject = {};
    for (const key of new Set([...Object.keys(state.loaded), ...Object.keys(state.draft)])) {
      if (key === '$schema') continue;
      const had = Object.hasOwn(state.loaded, key);
      const has = Object.hasOwn(state.draft, key);
      const current = state.draft[key];
      if (had && !has) values[key] = null;
      else if (has && current !== undefined && !jsonEqual(current, had ? state.loaded[key] : undefined)) values[key] = clone(current);
    }
    if (!Object.keys(values).length) return { kind: 'unchanged' };
    const snapshot = clone(state.draft);
    const body: ConfigObject = { base: state.etag, values };
    Object.defineProperty(body, Symbol.for('api.serialize'), { value: () => stringifyConfig(body) });
    state.saving = true;
    try {
      const res = await api('POST', '/api/config/set', body);
      if (!res) return { kind: 'network' };
      if (res.status === 409) { state.conflict = true; return { kind: 'conflict' }; }
      if (res.status !== 200 || res.success !== true) {
        const message = res.msg?.startsWith('Schema validation error:') ? '' : res.msg || '';
        return { kind: 'error', message };
      }
      if (Object.hasOwn(values, 'api_token')) { delete snapshot.api_token; delete state.draft.api_token; }
      state.loaded = snapshot;
      if (isPlainObject(res.obj) && typeof res.obj.base === 'string' && res.obj.base) state.etag = res.obj.base;
      state.conflict = false;
      if (Object.hasOwn(values, 'api_admin_ui_auth')) return { kind: 'reauth' };
      const restart = isPlainObject(res.obj) && Array.isArray(res.obj.restart) ? res.obj.restart.filter((key): key is string => typeof key === 'string') : [];
      return { kind: 'saved', message: res.msg || '', restart };
    } catch { return { kind: 'network' }; }
    finally { state.saving = false; }
  }
  return { state, load, save, dirty };
}

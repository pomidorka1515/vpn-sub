import { isRecord } from './json';
import type { Translate } from './formatting';

type Dynamic<T> = T | (() => T);
export interface ApiResult {
  [key: string]: unknown;
  status: number;
  success?: boolean;
  msg?: string;
  obj?: unknown;
  readonly rawJson?: string;
  etag?: string;
}
export interface ApiConfig {
  baseUrl?: Dynamic<string>;
  headers?: Dynamic<Record<string, string>>;
  guard?: () => boolean;
  guardMessage?: Dynamic<string>;
  on401?: () => void;
  networkError?: 'toast';
  networkMessage?: Dynamic<string>;
}
export interface ApiOptions { allow401?: (result: ApiResult) => boolean }

function resolve<T>(value: Dynamic<T> | undefined): T | undefined {
  return typeof value === 'function' ? (value as () => T)() : value;
}

// Validate the common envelope here. Page-specific `obj` data remains unknown
// until its consuming page validates it; no assertion pretends it is a DTO.
export function parseApiEnvelope(value: unknown): Record<string, unknown> {
  if (!isRecord(value) || (value.success !== undefined && typeof value.success !== 'boolean') ||
      (value.msg !== undefined && typeof value.msg !== 'string')) {
    throw new Error('Invalid API response');
  }
  return value;
}

export function serializeBody(body: unknown): string | undefined {
  if (typeof body === 'object' && body !== null) {
    const serialize: unknown = Reflect.get(body, Symbol.for('api.serialize'));
    if (typeof serialize === 'function') {
      const result: unknown = Reflect.apply(serialize, body, []);
      if (typeof result !== 'string') throw new Error('Invalid API serialization');
      return result;
    }
  }
  return JSON.stringify(body);
}

export function createApi(getConfig: () => ApiConfig, translate: Translate, toast: (message: string, type: string) => void, request: typeof fetch) {
  return async function api(method: string, path: string, body?: unknown, options: ApiOptions = {}): Promise<ApiResult | null> {
    const config = getConfig();
    if (config.guard?.()) {
      if (config.guardMessage) toast(resolve(config.guardMessage) ?? '', 'error');
      return null;
    }
    const headers = { ...resolve(config.headers) };
    const init: RequestInit = { method, credentials: 'same-origin', headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      const serialized = serializeBody(body);
      if (serialized !== undefined) init.body = serialized;
    }
    let response: Response;
    try {
      const base = (resolve(config.baseUrl) || '').replace(/\/+$/, '');
      response = await request(base + path, init);
    } catch (error) {
      if (config.networkError === 'toast') {
        toast(resolve(config.networkMessage) || translate('network_err'), 'error');
        return null;
      }
      throw error;
    }
    if (response.status === 204) return { success: true, status: 204 };
    let data: Record<string, unknown>;
    let rawJson = '';
    try {
      rawJson = await response.text();
      const parsed: unknown = JSON.parse(rawJson);
      data = parseApiEnvelope(parsed);
    } catch {
      data = { success: false, msg: response.statusText || translate('invalid_response') };
    }
    const result: ApiResult = { ...data, status: response.status };
    // Known envelope fields have been checked above; retain arbitrary fields
    // for existing endpoints without claiming their payload shape is validated.
    if (typeof data.success === 'boolean') result.success = data.success;
    if (typeof data.msg === 'string') result.msg = data.msg;
    if (response.status === 401 && config.on401 && !options.allow401?.(result)) {
      config.on401();
      return null;
    }
    Object.defineProperty(result, 'rawJson', { value: rawJson });
    const etag = response.headers.get('ETag');
    if (etag) result.etag = etag;
    return result;
  };
}

import { sharedMessage } from './messages';

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function stringTable(value: unknown): Record<string, string> {
  if (!isRecord(value) || Object.values(value).some(item => typeof item !== 'string')) {
    throw new Error(sharedMessage('invalid_translations'));
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, String(item)]));
}

import { sharedMessage } from './messages';
import { isRecord, stringTable } from './json';

export type Language = 'en' | 'ru';
export interface Bootstrap {
  prefix: string;
  language: Language;
  translations: Record<string, string>;
  fallback: Record<string, string>;
  languages?: Record<Language, Record<string, string>>;
  page: { chartUrl: string; debounceMs: number };
}

export function parseBootstrap(value: unknown): Bootstrap {
  if (
    !isRecord(value) ||
    typeof value['prefix'] !== 'string' ||
    (value['language'] !== 'en' && value['language'] !== 'ru') ||
    !isRecord(value['page']) ||
    typeof value['page']['chartUrl'] !== 'string' ||
    typeof value['page']['debounceMs'] !== 'number' ||
    !Number.isFinite(value['page']['debounceMs']) ||
    value['page']['debounceMs'] < 0
  ) {
    throw new Error(sharedMessage('invalid_bootstrap'));
  }
  return {
    prefix: value['prefix'],
    language: value['language'],
    translations: stringTable(value['translations']),
    fallback: stringTable(value['fallback']),
    ...(isRecord(value['languages'])
      ? { languages: { en: stringTable(value['languages']['en']), ru: stringTable(value['languages']['ru']) } }
      : {}),
    page: { chartUrl: value['page']['chartUrl'], debounceMs: value['page']['debounceMs'] },
  };
}

export function readBootstrap(doc: Document): Bootstrap {
  const source = doc.getElementById('frontend-bootstrap');
  if (!source?.textContent) throw new Error(sharedMessage('missing_bootstrap'));
  const value: unknown = JSON.parse(source.textContent);
  return parseBootstrap(value);
}

import translations, { errors } from 'virtual:shared-lang';

// Bundled from lang.jsonc so standalone pages and bootstrap failures work too.
export function sharedMessage(
  key: string,
  values: Record<string, string> = {},
  language = typeof document === 'undefined' ? 'en' : document.documentElement.lang,
): string {
  const table = language === 'ru' ? translations.ru : translations.en;
  const text = table[key] || translations.en[key] || key;
  return text.replace(/\{(\w+)\}/g, (match, name: string) => values[name] ?? match);
}

export function serverMessage(
  message: string | null | undefined,
  language = typeof document === 'undefined' ? 'en' : document.documentElement.lang,
  success = false,
): string {
  if (!message) return '';
  const table = language === 'ru' ? errors.ru : errors.en;
  // Schema diagnostics can quote whole objects containing credentials.
  if (message.startsWith('Schema validation error:'))
    return table['Schema validation error'] ?? sharedMessage('error', {}, language);
  const normalized = message.trim().replace(/\.$/, '');
  for (const [source, translated] of Object.entries(table)) {
    if (source.toLowerCase() === normalized.toLowerCase()) return translated;
    if (!source.includes('{')) continue;
    const names: string[] = [];
    const pattern = source
      .split(/(\{\w+\})/)
      .map((part) => {
        if (/^\{\w+\}$/.test(part)) {
          names.push(part.slice(1, -1));
          return '(.+?)';
        }
        return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      })
      .join('');
    const match = new RegExp('^' + pattern + '$', 'i').exec(normalized);
    if (match)
      return translated.replace(
        /\{(\w+)\}/g,
        (placeholder, name: string) => match[names.indexOf(name) + 1] ?? placeholder,
      );
  }
  // Never echo unrecognized server text, which may contain credentials.
  return success
    ? sharedMessage('ok', {}, language)
    : (table['Request failed'] ?? sharedMessage('error', {}, language));
}

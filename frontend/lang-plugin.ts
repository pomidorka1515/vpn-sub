import { readFileSync } from 'node:fs';
import ts from 'typescript';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

const path = new URL('../lang.jsonc', import.meta.url);
const id = 'virtual:shared-lang';
function source(): string {
  const parsed = ts.parseConfigFileTextToJson(path.pathname, readFileSync(path, 'utf8'));
  if (parsed.error) throw new Error(ts.flattenDiagnosticMessageText(parsed.error.messageText, '\n'));
  const config: unknown = parsed.config;
  if (!isRecord(config) || !isRecord(config['web'])) throw new Error('Invalid language configuration');
  return `export default ${JSON.stringify(config['web']['shared'])}; export const errors = ${JSON.stringify(config['web']['errors'])};`;
}

export const esbuildLang = {
  name: 'shared-lang',
  setup(build: import('esbuild').PluginBuild) {
    build.onResolve({ filter: /^virtual:shared-lang$/ }, () => ({ path: id, namespace: id }));
    build.onLoad({ filter: /.*/, namespace: id }, () => ({ contents: source(), loader: 'js', watchFiles: [path.pathname] }));
  },
};

export const viteLang = {
  name: 'shared-lang',
  resolveId(value: string) { if (value === id) return '\0' + id; return undefined; },
  load(value: string) { if (value === '\0' + id) return source(); return undefined; },
};

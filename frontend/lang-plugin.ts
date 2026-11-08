import { readFileSync } from 'node:fs';
import ts from 'typescript';

const path = new URL('../lang.jsonc', import.meta.url);
const id = 'virtual:shared-lang';
function source(): string {
  const parsed = ts.parseConfigFileTextToJson(path.pathname, readFileSync(path, 'utf8'));
  if (parsed.error) throw new Error(ts.flattenDiagnosticMessageText(parsed.error.messageText, '\n'));
  return `export default ${JSON.stringify(parsed.config.web.shared)}; export const errors = ${JSON.stringify(parsed.config.web.errors)};`;
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
  resolveId(value: string) { if (value === id) return '\0' + id; },
  load(value: string) { if (value === '\0' + id) return source(); },
};

import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test, { after, before } from 'node:test';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';

const root = dirname(fileURLToPath(import.meta.url));
const fixtures = {
  explicit: 'export const value = 1 as any;',
  unsafe: 'export const value: string = JSON.parse("null");',
  tsIgnore: '// @ts-ignore\nexport const value = 1;',
  eslintDisable: '// eslint-disable-next-line @typescript-eslint/no-explicit-any\nexport const value = 1 as any;',
  string: 'export const value = "any";',
};
let directory;
let eslint;

before(async () => {
  directory = await mkdtemp(join(tmpdir(), 'vpn-sub-eslint-'));
  await writeFile(
    join(directory, 'tsconfig.json'),
    JSON.stringify({
      extends: join(root, 'tsconfig.tests.json'),
      include: ['*.ts'],
      exclude: [],
    }),
  );
  await Promise.all(Object.entries(fixtures).map(([name, code]) => writeFile(join(directory, `${name}.ts`), code)));
  eslint = new ESLint({
    cwd: directory,
    overrideConfigFile: join(root, 'eslint.config.mjs'),
    overrideConfig: { languageOptions: { parserOptions: { project: './tsconfig.json', tsconfigRootDir: directory } } },
  });
});

after(async () => {
  if (directory) await rm(directory, { recursive: true, force: true });
});

async function rulesFor(name) {
  const [result] = await eslint.lintFiles(`${name}.ts`);
  assert.ok(result);
  assert.ok(
    result.messages.every((message) => !message.fatal),
    JSON.stringify(result.messages),
  );
  return result.messages.map((message) => message.ruleId);
}

test('rejects explicit any, including type assertions', async () => {
  assert.ok((await rulesFor('explicit')).includes('@typescript-eslint/no-explicit-any'));
});

test('rejects unsafe values and type checking suppressions', async () => {
  assert.ok((await rulesFor('unsafe')).includes('@typescript-eslint/no-unsafe-assignment'));
  assert.ok((await rulesFor('tsIgnore')).includes('@typescript-eslint/ban-ts-comment'));
  assert.ok((await rulesFor('eslintDisable')).includes('@typescript-eslint/no-explicit-any'));
});

test('allows any as an ordinary string', async () => {
  assert.deepEqual(await rulesFor('string'), []);
});

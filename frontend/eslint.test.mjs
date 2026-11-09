import assert from 'node:assert/strict';
import test from 'node:test';
import { ESLint } from 'eslint';

const eslint = new ESLint();
const filePath = 'src/shared/json.ts';

async function rulesFor(code) {
  const [result] = await eslint.lintText(code, { filePath });
  assert.ok(result);
  return result.messages.map((message) => message.ruleId);
}

test('rejects explicit any, including type assertions', async () => {
  assert.ok((await rulesFor('export const value = 1 as any;')).includes('@typescript-eslint/no-explicit-any'));
});

test('rejects unsafe values and type checking suppressions', async () => {
  assert.ok((await rulesFor('export const value: string = JSON.parse("null");')).includes('@typescript-eslint/no-unsafe-assignment'));
  assert.ok((await rulesFor('// @ts-ignore\nexport const value = 1;')).includes('@typescript-eslint/ban-ts-comment'));
  assert.ok((await rulesFor('// eslint-disable-next-line @typescript-eslint/no-explicit-any\nexport const value = 1 as any;')).includes('@typescript-eslint/no-explicit-any'));
});

test('allows any as an ordinary string', async () => {
  assert.deepEqual(await rulesFor('export const value = "any";'), []);
});

import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import { viteLang } from './lang-plugin';

export default defineConfig({
  plugins: [viteLang],
  root: fileURLToPath(new URL('.', import.meta.url)),
  test: {
    include: ['src/tests/**/*.test.ts'],
    environment: 'jsdom',
    environmentOptions: { jsdom: { url: 'https://example.test/' } },
    setupFiles: ['./src/tests/setup.ts'],
    clearMocks: true,
    restoreMocks: true,
    unstubGlobals: true,
    unstubEnvs: true,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.ts'],
      exclude: ['src/tests/**'],
      reportsDirectory: './coverage',
      reporter: ['text', 'html', 'lcov'],
    },
  },
});

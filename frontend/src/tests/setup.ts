import { afterEach, vi } from 'vitest';

afterEach(() => {
  vi.useRealTimers();
  if (typeof document === 'undefined') return;
  document.head.replaceChildren();
  document.body.replaceChildren();
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, '', '/');
});

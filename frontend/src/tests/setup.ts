import { afterEach, vi } from 'vitest';

// jsdom exposes dialog elements but does not implement their modal methods.
if (typeof HTMLDialogElement !== 'undefined') {
  HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  };
}

afterEach(() => {
  vi.useRealTimers();
  if (typeof document === 'undefined') return;
  document.head.replaceChildren();
  document.body.replaceChildren();
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, '', '/');
});

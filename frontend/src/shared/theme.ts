import { ICON_MOON, ICON_SUN } from './icons';
import type { StorageAccess } from './storage';

export type Theme = 'dark' | 'light';
export function createTheme(doc: Document, storage: StorageAccess) {
  let theme: Theme | undefined;
  const listeners = new Set<() => void>();
  function applyTheme(): void {
    theme ??= storage.getItem('theme') === 'light' ? 'light' : 'dark';
    doc.documentElement.setAttribute('data-theme', theme);
    const button = doc.getElementById('themeBtn');
    if (button && !doc.getElementById('themeIconDark')) button.innerHTML = ICON_MOON + ICON_SUN;
    const dark = doc.getElementById('themeIconDark');
    const light = doc.getElementById('themeIconLight');
    if (dark && light) {
      dark.style.display = theme === 'dark' ? 'block' : 'none';
      light.style.display = theme === 'light' ? 'block' : 'none';
    }
    listeners.forEach((listener) => {
      listener();
    });
  }
  function toggleTheme(): void {
    theme = (theme ?? 'dark') === 'dark' ? 'light' : 'dark';
    storage.setItem('theme', theme);
    applyTheme();
  }
  function onChange(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }
  return { applyTheme, toggleTheme, onChange };
}

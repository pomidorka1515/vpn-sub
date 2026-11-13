import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';

// This blocking head script applies the saved theme before visible content.
createTheme(
  document,
  safeStorage(() => localStorage),
).applyTheme();

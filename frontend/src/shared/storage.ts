export interface StorageAccess {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

// Privacy settings can deny even access to the browser's storage getter.
export function safeStorage(getStorage: () => Storage): StorageAccess {
  return {
    getItem(key) { try { return getStorage().getItem(key); } catch { return null; } },
    setItem(key, value) { try { getStorage().setItem(key, value); } catch { /* Storage is optional. */ } },
  };
}

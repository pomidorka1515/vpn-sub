type BusyButton = HTMLButtonElement | HTMLInputElement;
const depths = new WeakMap<BusyButton, number>();

export function busyDepth(button: BusyButton | null): number {
  return button ? depths.get(button) ?? 0 : 0;
}

export async function withBusy<T>(button: BusyButton | null, action: () => T | Promise<T>): Promise<T | undefined> {
  if (!button) return await action();
  if (button.disabled && !busyDepth(button)) return;
  depths.set(button, busyDepth(button) + 1);
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try { return await action(); }
  finally {
    const depth = busyDepth(button) - 1;
    if (depth) depths.set(button, depth);
    else {
      depths.delete(button);
      if (button.isConnected) {
        button.disabled = false;
        button.removeAttribute('aria-busy');
      }
    }
  }
}

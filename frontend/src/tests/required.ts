export function required<T>(value: T | null | undefined): T {
  if (value === null || value === undefined) throw new Error('Expected a value in test');
  return value;
}

export function requiredInstance<T>(value: unknown, constructor: abstract new (...args: never[]) => T): T {
  if (!(value instanceof constructor)) throw new Error(`Expected ${constructor.name} in test`);
  return value;
}

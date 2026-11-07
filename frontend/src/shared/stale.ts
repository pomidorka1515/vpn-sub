export function createLoadGeneration(): (key: string) => () => boolean {
  const generations = new Map<string, number>();
  return key => {
    const generation = (generations.get(key) ?? 0) + 1;
    generations.set(key, generation);
    return () => generations.get(key) === generation;
  };
}

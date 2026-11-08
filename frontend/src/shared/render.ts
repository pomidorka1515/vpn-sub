// Compare generated markup, not live markup: users may expand cards or focus controls.
const rendered = new WeakMap<HTMLElement, string>();

export function renderIfChanged(element: HTMLElement, html: string): void {
  if (rendered.get(element) === html) return;
  element.innerHTML = html;
  rendered.set(element, html);
}

/**
 * Accessibility helpers applied to the whole document.
 *
 * Forms throughout the app use the layout `<label class="l">Name</label><input …/>` (label and
 * control are siblings in a CSS grid) or `<label class="l">…</label><div><input …/>…</div>`.
 * linkLabels() associates each such label with its control (id + htmlFor), so screen readers
 * announce the field name and clicking the label focuses the field. A MutationObserver keeps doing
 * this for dialogs and panels rendered later.
 */
let seq = 0;
const CONTROL = "input:not([type=hidden]), select, textarea";

function controlFor(label: HTMLLabelElement): HTMLElement | null {
  const next = label.nextElementSibling as HTMLElement | null;
  if (!next) return null;
  if (next.matches(CONTROL)) return next;
  if (next.tagName === "LABEL") return null;
  return next.querySelector<HTMLElement>(CONTROL);
}

export function linkLabels(root: ParentNode = document) {
  root.querySelectorAll<HTMLLabelElement>("label.l:not([for])").forEach((label) => {
    const control = controlFor(label);
    if (!control) return;
    if (!control.id) control.id = `fld-${++seq}`;
    label.htmlFor = control.id;
  });
}

export function installA11y() {
  linkLabels();
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; linkLabels(); });
  }).observe(document.body, { childList: true, subtree: true });
}

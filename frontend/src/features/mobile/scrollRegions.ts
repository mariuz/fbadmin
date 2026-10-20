/**
 * On phones and tablets many tables are wider than the screen and scroll inside their own box.
 * A scrolling box without anything focusable in it cannot be scrolled from the keyboard (WCAG
 * 2.1.1, axe "scrollable-region-focusable"), so such boxes get tabindex=0 while they overflow.
 * Desktop layouts are left alone.
 */
import { currentLayout } from "./responsive";

const BOXES = ".grid-wrap, .log, .qt-out";
const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
const MARK = "data-scroll-focus";

function update() {
  const small = currentLayout() !== "desktop";
  document.querySelectorAll<HTMLElement>(BOXES).forEach((el) => {
    const ours = el.hasAttribute(MARK);
    const overflows = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
    const need = small && overflows && !el.querySelector(FOCUSABLE);
    if (need && !ours && !el.hasAttribute("tabindex")) {
      el.setAttribute("tabindex", "0");
      el.setAttribute(MARK, "");
    } else if (!need && ours) {
      el.removeAttribute("tabindex");
      el.removeAttribute(MARK);
    }
  });
}

export function installScrollRegions() {
  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; update(); });
  };
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  window.addEventListener("resize", schedule);
}

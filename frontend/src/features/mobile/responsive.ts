/**
 * Phone and tablet layout: breakpoints and small hooks for the few places where behaviour (not
 * just CSS) changes with the screen size. Layout itself lives in mobile.css (media queries).
 *
 *   phone    < 640 px   Object Explorer as an off-canvas drawer, topbar overflow menu, sheets
 *   tablet   640–1023   collapsible sidebar, topbar overflow menu (touch screens: up to 1366 px)
 *   desktop  ≥ 1024 px  unchanged (with a mouse)
 */
import { useCallback, useRef, useSyncExternalStore } from "react";

export const PHONE_QUERY = "(max-width: 639.98px)";
// touch screens up to 1366 px (iPad landscape, iPad Pro) also get the tablet layout: the desktop
// topbar does not fit there with touch-sized targets. Desktops with a mouse are not affected.
export const TABLET_QUERY = "(min-width: 640px) and (max-width: 1023.98px), (min-width: 640px) and (max-width: 1366px) and (pointer: coarse)";
export const COARSE_QUERY = "(pointer: coarse)";

export type Layout = "phone" | "tablet" | "desktop";

function matches(q: string): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(q).matches;
}

export function currentLayout(): Layout {
  return matches(PHONE_QUERY) ? "phone" : matches(TABLET_QUERY) ? "tablet" : "desktop";
}

// one MediaQueryList per query, shared by every component that asks
const lists = new Map<string, MediaQueryList>();
function list(q: string): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  let l = lists.get(q);
  if (!l) lists.set(q, (l = window.matchMedia(q)));
  return l;
}

export function useMediaQuery(q: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const l = list(q);
      l?.addEventListener("change", cb);
      return () => l?.removeEventListener("change", cb);
    },
    () => list(q)?.matches ?? false,
    () => false,
  );
}

export function useLayout(): Layout {
  const phone = useMediaQuery(PHONE_QUERY);
  const tablet = useMediaQuery(TABLET_QUERY);
  return phone ? "phone" : tablet ? "tablet" : "desktop";
}

/** Touch screens (phones, tablets, touch laptops): bigger targets and touch-only controls. */
export function useCoarsePointer(): boolean {
  return useMediaQuery(COARSE_QUERY);
}

const LONG_PRESS_MS = 500;
const MOVE_TOLERANCE = 10;

/**
 * Long-press (touch) handlers that open a context menu: hold still for half a second. Moving the
 * finger (scrolling) cancels it. While held, ``onPressing(true)`` lets the element show a hint.
 * The touch that ends a long press is swallowed so it does not also click or close the menu, and
 * a native ``contextmenu`` event (Android Chrome sends one too) is not handled twice — use
 * ``shouldIgnoreContextMenu`` in the element's onContextMenu.
 */
export function useLongPress(onLongPress: (x: number, y: number, target: HTMLElement) => void, onPressing?: (on: boolean) => void) {
  const timer = useRef<number | undefined>(undefined);
  const start = useRef<{ x: number; y: number } | null>(null);
  const firedAt = useRef(0);
  const cb = useRef(onLongPress);
  cb.current = onLongPress;
  const pressing = useRef(onPressing);
  pressing.current = onPressing;

  const cancel = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = undefined;
    start.current = null;
    pressing.current?.(false);
  }, []);

  const onTouchStart = useCallback((e: React.TouchEvent<HTMLElement>) => {
    if (e.touches.length !== 1) return cancel();
    const tch = e.touches[0];
    const target = e.currentTarget;
    start.current = { x: tch.clientX, y: tch.clientY };
    pressing.current?.(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const p = start.current;
      timer.current = undefined;
      pressing.current?.(false);
      if (!p) return;
      firedAt.current = Date.now();
      try { navigator.vibrate?.(15); } catch { /* not allowed */ }
      cb.current(p.x, p.y, target);
    }, LONG_PRESS_MS);
  }, [cancel]);

  const onTouchMove = useCallback((e: React.TouchEvent<HTMLElement>) => {
    const p = start.current;
    const tch = e.touches[0];
    if (p && tch && Math.hypot(tch.clientX - p.x, tch.clientY - p.y) > MOVE_TOLERANCE) cancel();
  }, [cancel]);

  const onTouchEnd = useCallback((e: React.TouchEvent<HTMLElement>) => {
    cancel();
    // no click / mouse events after a long press (they would select again or close the menu)
    if (Date.now() - firedAt.current < 1500 && e.cancelable) e.preventDefault();
  }, [cancel]);

  /** True when a contextmenu event belongs to a long press that already opened the menu. */
  const shouldIgnoreContextMenu = useCallback(() => {
    if (timer.current !== undefined) {
      // the browser was first (Android): let it open the menu, and swallow the touch end
      cancel();
      firedAt.current = Date.now();
      return false;
    }
    return Date.now() - firedAt.current < 1500;
  }, [cancel]);

  return { handlers: { onTouchStart, onTouchMove, onTouchEnd, onTouchCancel: cancel }, shouldIgnoreContextMenu };
}

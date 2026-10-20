/**
 * App-shell pieces for phones and tablets (see responsive.ts for the breakpoints):
 * - TopbarOverflow: the "⋯" button that holds the topbar buttons that do not fit;
 * - useNavDrawer: the Object Explorer as an off-canvas drawer (phone) or a collapsible sidebar
 *   (tablet), with focus handling, Escape, tap outside and swipe to close, and an inert background.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Modal } from "../../components/Modal";
import { t } from "../../i18n";
import type { Layout } from "./responsive";

/** "⋯" topbar button: a sheet with the topbar buttons that do not fit on a small screen. */
export function TopbarOverflow(props: { badge?: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  // a button in the sheet does its job (its own handler runs first: bubbling) and closes the sheet, unless it opens a menu of its own (a disclosure button)
  const onClickItem = (e: React.MouseEvent) => {
    const b = (e.target as Element).closest("button");
    if (b && !b.disabled && !b.hasAttribute("aria-haspopup") && !b.hasAttribute("aria-expanded")) setOpen(false);
  };
  return (
    <>
      <button className="tb tb-more" aria-haspopup="dialog" aria-expanded={open} title={t("More")} aria-label={t("More")} onClick={() => setOpen(true)}>
        <span aria-hidden="true" className="tb-more-dots">⋯</span>{props.badge}
      </button>
      {/* portal: outside the topbar's colours and layout */}
      {open && createPortal(
        <Modal title={t("Menu")} onClose={close} className="sheet topbar-sheet">
          <div className="topbar-sheet-items" onClick={onClickItem}>{props.children}</div>
        </Modal>,
        document.body,
      )}
    </>
  );
}

const COLLAPSED_KEY = "fbadmin.sidebarCollapsed";

function loadCollapsed(): boolean {
  try { return localStorage.getItem(COLLAPSED_KEY) === "1"; } catch { return false; }
}

/**
 * State and behaviour of the Object Explorer on small screens. Phone: a drawer that is closed by
 * default; tablet: a sidebar that can be collapsed (remembered); desktop: always shown.
 */
export function useNavDrawer(layout: Layout) {
  const [drawer, setDrawer] = useState(false);
  const [collapsed, setCollapsedState] = useState(loadCollapsed);
  const navRef = useRef<HTMLElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const phone = layout === "phone";
  const open = phone ? drawer : layout === "tablet" ? !collapsed : true;

  const setCollapsed = (v: boolean) => {
    setCollapsedState(v);
    try { localStorage.setItem(COLLAPSED_KEY, v ? "1" : "0"); } catch { /* storage unavailable */ }
  };
  const toggle = () => (phone ? setDrawer((d) => !d) : setCollapsed(!collapsed));
  const closeDrawer = useCallback(() => setDrawer(false), []);

  // leaving the phone layout (rotation, resize) closes the drawer
  useEffect(() => { if (!phone) setDrawer(false); }, [phone]);

  // inert background while the drawer is open, inert drawer while it is closed
  useEffect(() => {
    const nav = navRef.current;
    const app = nav?.closest(".app");
    const background = app ? Array.from(app.querySelectorAll<HTMLElement>(":scope > .topbar, :scope > .main > .workspace")) : [];
    background.forEach((el) => { el.inert = phone && drawer; });
    if (nav) nav.inert = phone && !drawer;
    return () => {
      background.forEach((el) => { el.inert = false; });
      if (nav) nav.inert = false;
    };
  }, [phone, drawer]);

  // focus moves into the drawer when it opens and back to the toggle button when it closes
  const wasOpen = useRef(false);
  useEffect(() => {
    if (!phone) { wasOpen.current = false; return; }
    const nav = navRef.current;
    if (drawer && nav) {
      const target = nav.querySelector<HTMLElement>('.tree-row[tabindex="0"]') ?? nav.querySelector<HTMLElement>(".tree-row")
        ?? nav.querySelector<HTMLElement>("button");
      // after the slide-in starts: focusing a hidden (visibility) element would fail
      requestAnimationFrame(() => target?.focus({ preventScroll: true }));
    } else if (wasOpen.current) {
      toggleRef.current?.focus({ preventScroll: true });
    }
    wasOpen.current = drawer;
  }, [phone, drawer]);

  // Escape closes the drawer (unless a menu or dialog on top of it handles it)
  useEffect(() => {
    if (!phone || !drawer) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || document.querySelector(".ctx, .modal")) return;
      setDrawer(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phone, drawer]);

  // swipe left closes the drawer (but not when the gesture scrolled the tree sideways)
  const swipe = useRef<{ x: number; y: number; scroll: number } | null>(null);
  const navTouch = phone ? {
    onTouchStart: (e: React.TouchEvent) => {
      const tree = navRef.current?.querySelector(".tree");
      swipe.current = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY, scroll: tree?.scrollLeft ?? 0 } : null;
    },
    onTouchEnd: (e: React.TouchEvent) => {
      const s = swipe.current;
      swipe.current = null;
      const tch = e.changedTouches[0];
      if (!s || !tch) return;
      const dx = tch.clientX - s.x, dy = tch.clientY - s.y;
      const tree = navRef.current?.querySelector(".tree");
      if (dx < -60 && Math.abs(dx) > 1.5 * Math.abs(dy) && (tree?.scrollLeft ?? 0) === s.scroll) setDrawer(false);
    },
  } : {};

  return { open, drawer, phone, toggle, closeDrawer, setDrawer, navRef, toggleRef, navTouch };
}

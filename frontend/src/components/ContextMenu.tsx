import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export type MenuItem =
  | { label: string; icon?: ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }
  | { label: string; icon?: ReactNode; children: MenuItem[] }
  | { separator: true }
  | { header: string };

/** Menus longer than this collapse each titled section into a submenu. */
const MAX_FLAT = 18;

function titleCase(s: string) {
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

/** Turn "header + items" sections into submenus when a menu is too long to fit. */
export function groupLongMenu(items: MenuItem[]): MenuItem[] {
  const count = items.filter((i) => "label" in i).length;
  if (count <= MAX_FLAT) return items;
  const out: MenuItem[] = [];
  let group: { label: string; children: MenuItem[] } | null = null;
  for (const it of items) {
    if ("header" in it) {
      group = { label: titleCase(it.header), children: [] };
      out.push(group);
    } else if ("separator" in it) {
      group = null;
      out.push(it);
    } else if (group) group.children.push(it);
    else out.push(it);
  }
  // drop separators that ended up adjacent / leading / trailing
  return out.filter((it, i, a) => !("separator" in it) || (i > 0 && i < a.length - 1 && !("separator" in a[i - 1])));
}

function MenuList(props: { items: MenuItem[]; onClose: () => void; x: number; y: number; sub?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x: props.x, y: props.y });
  const [open, setOpenNow] = useState<number | null>(null);
  // hover intent: switching/closing submenus waits a moment so the pointer can travel
  // diagonally from the parent item into the submenu without closing it
  const timer = useRef<number | undefined>(undefined);
  const setOpen = (v: number | null | ((o: number | null) => number | null), delay = 0) => {
    window.clearTimeout(timer.current);
    if (delay) timer.current = window.setTimeout(() => setOpenNow(v), delay);
    else setOpenNow(v);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    let x = props.x;
    if (x + r.width > window.innerWidth - 8) x = props.sub ? props.x - r.width - (ref.current?.parentElement?.getBoundingClientRect().width ?? 0) : window.innerWidth - r.width - 8;
    setPos({ x: Math.max(8, x), y: Math.max(8, Math.min(props.y, window.innerHeight - r.height - 8)) });
  }, []);
  return (
    <div className="ctx" ref={ref} role="menu" style={{ left: pos.x, top: pos.y }} onContextMenu={(e) => e.preventDefault()}>
      {props.items.map((it, i) => {
        if ("separator" in it) return <hr key={i} />;
        if ("header" in it) return <div key={i} className="sub-h">{it.header}</div>;
        if ("children" in it) {
          return (
            <div key={i} className="ctx-sub" onMouseEnter={() => setOpen(i, open === null ? 0 : 250)} onMouseLeave={() => setOpen((o) => (o === i ? null : o), 300)}>
              <button role="menuitem" aria-haspopup="menu" aria-expanded={open === i}
                onClick={() => setOpen(open === i ? null : i)}
                onKeyDown={(e) => e.key === "ArrowRight" && setOpen(i)}>
                {it.icon}{it.label}<span className="ctx-arrow">▸</span>
              </button>
              {open === i && (
                <SubMenu items={it.children} onClose={props.onClose} />
              )}
            </div>
          );
        }
        return (
          <button key={i} role="menuitem" className={it.danger ? "danger" : ""} disabled={it.disabled}
            onMouseEnter={() => setOpen(null, 300)}
            onClick={() => { props.onClose(); it.onClick(); }}>
            {it.icon}
            {it.label}
          </button>
        );
      })}
    </div>
  );
}

function SubMenu(props: { items: MenuItem[]; onClose: () => void }) {
  const anchor = useRef<HTMLSpanElement>(null);
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  useLayoutEffect(() => {
    const r = anchor.current?.parentElement?.getBoundingClientRect();
    if (r) setAt({ x: r.right - 2, y: r.top - 4 });
  }, []);
  return <span ref={anchor}>{at && <MenuList items={props.items} onClose={props.onClose} x={at.x} y={at.y} sub />}</span>;
}

export function ContextMenu(props: { x: number; y: number; items: MenuItem[]; onClose: () => void }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) props.onClose();
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && props.onClose();
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", esc);
    window.addEventListener("blur", props.onClose);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", esc);
      window.removeEventListener("blur", props.onClose);
    };
  }, []);
  return (
    <div ref={root}>
      <MenuList items={groupLongMenu(props.items)} onClose={props.onClose} x={props.x} y={props.y} />
    </div>
  );
}

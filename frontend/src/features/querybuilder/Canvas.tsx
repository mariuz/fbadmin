/**
 * Query builder canvas: table boxes (drag the header to move, tick columns for the output),
 * join lines with a button per join, drag a column onto a column of another table to join them,
 * drop tables and views from the Object Explorer. Everything here can also be done in the grids.
 *
 * Touch: boxes are dragged by their header with a finger, two fingers pinch-zoom (as do the zoom
 * buttons), and since HTML drag and drop does not work with touch, columns are joined by tapping the
 * join button of one column and then of a column in another table.
 */
import { useEffect, useRef, useState } from "react";
import { t } from "../../i18n";
import { IconKey, IconX } from "../../components/Icons";
import { useCoarsePointer } from "../mobile/responsive";
import type { QbJoin, QbState, QbTable } from "./model";

export const BOX_W = 220;
const HEAD = 30, ROW = 22;
export const REL_MIME = "application/x-fbadmin-object";
const COL_MIME = "application/x-fbadmin-qb-column";

export const TOUCH_ROW = 40; // column rows on touch screens (room for the join button)

export function boxHeight(tb: QbTable, row = ROW) {
  return HEAD + (tb.columns.length + 1) * row + 6;
}
function colY(tb: QbTable, col: string, row = ROW) {
  const i = tb.columns.findIndex((c) => c.name === col);
  return i < 0 ? tb.y + HEAD / 2 : tb.y + HEAD + (i + 1) * row + row / 2;
}

export function Canvas(props: {
  st: QbState;
  update: (fn: (s: QbState) => QbState) => void;
  onDropRelation: (rel: { name: string; schema?: string | null; kind: "table" | "view" }, x: number, y: number) => void;
  onToggleColumn: (tableId: string, column: string) => void;
  onRemoveTable: (id: string) => void;
  onJoinColumns: (a: { table: string; column: string }, b: { table: string; column: string }) => void;
  onEditJoin: (id: string) => void;
  activeJoin: string | null;
}) {
  const { st } = props;
  const wrap = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: string; sx: number; sy: number; ox: number; oy: number } | null>(null);
  const [zoom, setZoom] = useState(1);
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const coarse = useCoarsePointer();
  const [joinFrom, setJoinFrom] = useState<{ table: string; column: string } | null>(null);
  const clampZoom = (z: number) => Math.min(1.5, Math.max(0.4, Math.round(z * 100) / 100));

  // two-finger pinch (touch events: one finger keeps scrolling the canvas natively)
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    let start: { d: number; z: number } | null = null;
    const dist = (e: TouchEvent) => Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY) || 1;
    const onStart = (e: TouchEvent) => { if (e.touches.length === 2) start = { d: dist(e), z: zoomRef.current }; };
    const onMove = (e: TouchEvent) => {
      if (!start || e.touches.length !== 2) return;
      e.preventDefault();
      setZoom(clampZoom(start.z * (dist(e) / start.d)));
    };
    const onEnd = (e: TouchEvent) => { if (e.touches.length < 2) start = null; };
    el.addEventListener("touchstart", onStart, { passive: true });
    el.addEventListener("touchmove", onMove, { passive: false });
    el.addEventListener("touchend", onEnd);
    el.addEventListener("touchcancel", onEnd);
    return () => {
      el.removeEventListener("touchstart", onStart);
      el.removeEventListener("touchmove", onMove);
      el.removeEventListener("touchend", onEnd);
      el.removeEventListener("touchcancel", onEnd);
    };
  }, []);

  const tapJoin = (table: string, column: string) => {
    if (!joinFrom) setJoinFrom({ table, column });
    else if (joinFrom.table === table) setJoinFrom(joinFrom.column === column ? null : { table, column });
    else {
      props.onJoinColumns(joinFrom, { table, column });
      setJoinFrom(null);
    }
  };
  const byId = new Map(st.tables.map((x) => [x.id, x]));
  const outputCols = new Set(st.fields.filter((f) => f.output && f.table).map((f) => `${f.table}\u0001${f.column}`));
  const width = Math.max(800, ...st.tables.map((x) => x.x + BOX_W + 200));
  const row = coarse ? TOUCH_ROW : ROW;
  const height = Math.max(400, ...st.tables.map((x) => x.y + boxHeight(x, row) + 100));

  const joinGeometry = (j: QbJoin) => {
    const a = byId.get(j.left), b = byId.get(j.right);
    if (!a || !b) return null;
    const conds = j.conditions.length && !j.custom.trim() ? j.conditions : [{ left: "", op: "=", right: "" }];
    const aLeft = a.x + BOX_W / 2 <= b.x + BOX_W / 2;
    const paths = conds.map((c) => {
      const x1 = aLeft ? a.x + BOX_W : a.x, x2 = aLeft ? b.x : b.x + BOX_W;
      const y1 = colY(a, c.left, row), y2 = colY(b, c.right, row);
      const dx = Math.max(40, Math.abs(x2 - x1) / 2) * (aLeft ? 1 : -1);
      return { d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 };
    });
    return { paths, label: { x: paths[0].mx, y: paths[0].my } };
  };

  const onDrop = (e: React.DragEvent) => {
    const raw = e.dataTransfer.getData(REL_MIME);
    if (!raw) return;
    e.preventDefault();
    try {
      const sel = JSON.parse(raw) as { name?: string; otype?: string; schema?: string };
      if (!sel.name || !["tables", "views"].includes(sel.otype ?? "")) return;
      const r = wrap.current!.getBoundingClientRect();
      props.onDropRelation({ name: sel.name, schema: sel.schema ?? null, kind: sel.otype === "views" ? "view" : "table" },
        Math.max(10, (e.clientX - r.left + wrap.current!.scrollLeft) / zoom - 40),
        Math.max(10, (e.clientY - r.top + wrap.current!.scrollTop) / zoom - 10));
    } catch { /* not ours */ }
  };

  const joinFromLabel = joinFrom && (() => {
    const tb = byId.get(joinFrom.table);
    return `${tb?.alias || tb?.name}.${joinFrom.column}`;
  })();
  return (
    <div className="qb-canvas-wrap">
    <div className="qb-zoom" role="group" aria-label={t("Zoom")}>
      <button className="btn icon small" aria-label={t("Zoom out")} title={t("Zoom out")} disabled={zoom <= 0.4}
        onClick={() => setZoom((z) => clampZoom(z - 0.1))}>－</button>
      <button className="btn small" title={t("Actual size")} onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
      <button className="btn icon small" aria-label={t("Zoom in")} title={t("Zoom in")} disabled={zoom >= 1.5}
        onClick={() => setZoom((z) => clampZoom(z + 0.1))}>＋</button>
    </div>
    {joinFrom && (
      <div className="qb-join-hint" role="status">
        {t("Tap the join button of a column in another table to join it with {column}", { column: joinFromLabel ?? "" })}
        <button className="btn small" onClick={() => setJoinFrom(null)}>{t("Cancel")}</button>
      </div>
    )}
    <div className={"qb-canvas" + (coarse ? " touch" : "")} ref={wrap}
      onDragOver={(e) => { if (e.dataTransfer.types.includes(REL_MIME)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } }}
      onDrop={onDrop}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        const nx = Math.max(0, d.ox + (e.clientX - d.sx) / zoom), ny = Math.max(0, d.oy + (e.clientY - d.sy) / zoom);
        props.update((s) => ({ ...s, tables: s.tables.map((x) => (x.id === d.id ? { ...x, x: nx, y: ny } : x)) }));
      }}
      onPointerUp={() => { drag.current = null; }}
      onPointerCancel={() => { drag.current = null; }}>
      <div style={{ width: width * zoom, height: height * zoom }}>
      <div style={{ position: "relative", width, height, transform: zoom === 1 ? undefined : `scale(${zoom})`, transformOrigin: "0 0" }}>
        <svg className="qb-lines" width={width} height={height} aria-hidden="true">
          {st.joins.map((j) => {
            const g = joinGeometry(j);
            return g?.paths.map((p, k) => (
              <path key={`${j.id}-${k}`} d={p.d} className={"qb-line" + (j.id === props.activeJoin ? " active" : "") + (j.type !== "INNER" ? " outer" : "")} />
            ));
          })}
        </svg>
        {st.joins.map((j) => {
          const g = joinGeometry(j);
          const a = byId.get(j.left), b = byId.get(j.right);
          if (!g || !a || !b) return null;
          return (
            <button key={j.id} className={"qb-join-label" + (j.id === props.activeJoin ? " active" : "")} style={{ left: g.label.x, top: g.label.y }}
              title={t("Edit this join")} aria-label={t("{type} join {left} – {right}: edit", { type: j.type, left: a.alias || a.name, right: b.alias || b.name })}
              onClick={() => props.onEditJoin(j.id)}>
              {j.type}
            </button>
          );
        })}
        {st.tables.map((tb) => (
          <div key={tb.id} className="qb-box" style={{ left: tb.x, top: tb.y, width: BOX_W }} data-qb-table={tb.name}>
            <div className="qb-box-head" title={t("Drag to move")}
              onPointerDown={(e) => {
                if ((e.target as HTMLElement).closest("button")) return;
                e.preventDefault();
                (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId); // a finger may leave the header
                drag.current = { id: tb.id, sx: e.clientX, sy: e.clientY, ox: tb.x, oy: tb.y };
              }}
              onPointerMove={(e) => {
                const d = drag.current;
                if (!d || d.id !== tb.id) return;
                const nx = Math.max(0, d.ox + (e.clientX - d.sx) / zoom), ny = Math.max(0, d.oy + (e.clientY - d.sy) / zoom);
                props.update((s) => ({ ...s, tables: s.tables.map((x) => (x.id === d.id ? { ...x, x: nx, y: ny } : x)) }));
              }}
              onPointerUp={() => { drag.current = null; }}>
              <span className="qb-alias">{tb.alias}</span>
              <span className="qb-name" title={(tb.schema ? `${tb.schema}.` : "") + tb.name}>{tb.schema ? <span className="muted">{tb.schema}.</span> : null}{tb.name}</span>
              {tb.kind === "view" && <span className="muted">{t("view")}</span>}
              <button className="btn icon small" aria-label={t("Remove {table}", { table: tb.alias || tb.name })} title={t("Remove from the query")}
                onClick={() => props.onRemoveTable(tb.id)}><IconX size={11} /></button>
            </div>
            <ul className="qb-cols">
              <li className="qb-col star">
                <label><input type="checkbox" checked={outputCols.has(`${tb.id}\u0001*`)} onChange={() => props.onToggleColumn(tb.id, "*")} /> {t("* (all columns)")}</label>
              </li>
              {tb.columns.map((c) => (
                <li key={c.name} className="qb-col" draggable
                  onDragStart={(e) => { e.dataTransfer.setData(COL_MIME, JSON.stringify({ table: tb.id, column: c.name })); e.dataTransfer.effectAllowed = "link"; }}
                  onDragOver={(e) => { if (e.dataTransfer.types.includes(COL_MIME)) { e.preventDefault(); e.dataTransfer.dropEffect = "link"; } }}
                  onDrop={(e) => {
                    const raw = e.dataTransfer.getData(COL_MIME);
                    if (!raw) return;
                    e.preventDefault();
                    e.stopPropagation();
                    const from = JSON.parse(raw) as { table: string; column: string };
                    if (from.table !== tb.id) props.onJoinColumns(from, { table: tb.id, column: c.name });
                  }}
                  title={t("Drag onto a column of another table to join on it")}>
                  <label>
                    <input type="checkbox" checked={outputCols.has(`${tb.id}\u0001${c.name}`)} onChange={() => props.onToggleColumn(tb.id, c.name)} />
                    {" "}<span className="qb-cname">{c.name}</span>
                  </label>
                  {(c.pk || c.fk) && <span className={c.pk ? "qb-pk" : "qb-fk"} title={c.pk ? t("Primary key") : t("Foreign key")}><IconKey size={11} /></span>}
                  <span className="qb-type">{c.type.toLowerCase()}</span>
                  {coarse && (
                    <button type="button" className={"qb-join-btn" + (joinFrom?.table === tb.id && joinFrom.column === c.name ? " on" : "")}
                      aria-pressed={joinFrom?.table === tb.id && joinFrom.column === c.name}
                      aria-label={t("Join on {table}.{column}", { table: tb.alias || tb.name, column: c.name })}
                      onClick={() => tapJoin(tb.id, c.name)}>⇄</button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {!st.tables.length && (
          <div className="qb-hint">{t("Add tables and views with “Add table” above, or drag them here from the Object Explorer.")}</div>
        )}
      </div>
      </div>
    </div>
    </div>
  );
}

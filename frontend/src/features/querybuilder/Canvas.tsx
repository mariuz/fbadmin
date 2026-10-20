/**
 * Query builder canvas: table boxes (drag the header to move, tick columns for the output),
 * join lines with a button per join, drag a column onto a column of another table to join them,
 * drop tables and views from the Object Explorer. Everything here can also be done in the grids.
 */
import { useRef } from "react";
import { t } from "../../i18n";
import { IconKey, IconX } from "../../components/Icons";
import type { QbJoin, QbState, QbTable } from "./model";

export const BOX_W = 220;
const HEAD = 30, ROW = 22;
export const REL_MIME = "application/x-fbadmin-object";
const COL_MIME = "application/x-fbadmin-qb-column";

export function boxHeight(tb: QbTable) {
  return HEAD + (tb.columns.length + 1) * ROW + 6;
}
function colY(tb: QbTable, col: string) {
  const i = tb.columns.findIndex((c) => c.name === col);
  return i < 0 ? tb.y + HEAD / 2 : tb.y + HEAD + (i + 1) * ROW + ROW / 2;
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
  const byId = new Map(st.tables.map((x) => [x.id, x]));
  const outputCols = new Set(st.fields.filter((f) => f.output && f.table).map((f) => `${f.table}\u0001${f.column}`));
  const width = Math.max(800, ...st.tables.map((x) => x.x + BOX_W + 200));
  const height = Math.max(400, ...st.tables.map((x) => x.y + boxHeight(x) + 100));

  const joinGeometry = (j: QbJoin) => {
    const a = byId.get(j.left), b = byId.get(j.right);
    if (!a || !b) return null;
    const conds = j.conditions.length && !j.custom.trim() ? j.conditions : [{ left: "", op: "=", right: "" }];
    const aLeft = a.x + BOX_W / 2 <= b.x + BOX_W / 2;
    const paths = conds.map((c) => {
      const x1 = aLeft ? a.x + BOX_W : a.x, x2 = aLeft ? b.x : b.x + BOX_W;
      const y1 = colY(a, c.left), y2 = colY(b, c.right);
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
        Math.max(10, e.clientX - r.left + wrap.current!.scrollLeft - 40), Math.max(10, e.clientY - r.top + wrap.current!.scrollTop - 10));
    } catch { /* not ours */ }
  };

  return (
    <div className="qb-canvas" ref={wrap}
      onDragOver={(e) => { if (e.dataTransfer.types.includes(REL_MIME)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } }}
      onDrop={onDrop}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        const nx = Math.max(0, d.ox + e.clientX - d.sx), ny = Math.max(0, d.oy + e.clientY - d.sy);
        props.update((s) => ({ ...s, tables: s.tables.map((x) => (x.id === d.id ? { ...x, x: nx, y: ny } : x)) }));
      }}
      onPointerUp={() => { drag.current = null; }}>
      <div style={{ position: "relative", width, height }}>
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
                drag.current = { id: tb.id, sx: e.clientX, sy: e.clientY, ox: tb.x, oy: tb.y };
              }}>
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
  );
}

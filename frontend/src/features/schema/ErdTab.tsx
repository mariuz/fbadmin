import { useEffect, useMemo, useRef, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { IconDownload, IconKey, IconPlus, IconRefresh, IconTerminal, IconTrash } from "../../components/Icons";
import { buildErdSvg, ErdExportDialog } from "./ErdExport";
import { schemaApi, type ErdColumn, type ErdRelation } from "./api";
import { quoteIdent } from "./common";
import { t } from "../../i18n";
import { useCoarsePointer } from "../mobile/responsive";

// ------------------------------------------------------------------ model
interface Col extends Partial<ErdColumn> { name: string; type: string; isNew?: boolean }
interface Node { name: string; x: number; y: number; columns: Col[]; isNew?: boolean; comment?: string | null; gtt?: boolean }
interface Rel extends ErdRelation { isNew?: boolean }

const CHAR_W = 7.2, HEAD_H = 28, ROW_H = 20, PAD = 10, GAP_X = 90, GAP_Y = 36;
const TYPES = ["INTEGER", "BIGINT", "SMALLINT", "VARCHAR(50)", "VARCHAR(255)", "CHAR(10)", "NUMERIC(15,2)", "DOUBLE PRECISION",
  "DATE", "TIME", "TIMESTAMP", "BOOLEAN", "BLOB SUB_TYPE TEXT", "BLOB SUB_TYPE BINARY"];

function visibleCols(n: Node, keysOnly: boolean) {
  return keysOnly ? n.columns.filter((c) => c.pk || c.fk || c.isNew) : n.columns;
}
function nodeW(n: Node) {
  const longest = Math.max(n.name.length + 4, ...n.columns.map((c) => c.name.length + c.type.length + 3));
  return Math.max(170, Math.min(420, Math.round(longest * CHAR_W + 2 * PAD + 22)));
}
function nodeH(n: Node, keysOnly: boolean) {
  return HEAD_H + Math.max(1, visibleCols(n, keysOnly).length) * ROW_H + 6;
}

/** Layered layout: referenced (parent) tables on the left, children to the right; isolated tables in a grid. */
function autoLayout(nodes: Node[], rels: Rel[], keysOnly: boolean): Node[] {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const parents = new Map<string, Set<string>>();
  const linked = new Set<string>();
  for (const r of rels) {
    if (r.from_table === r.to_table || !byName.has(r.from_table) || !byName.has(r.to_table)) continue;
    (parents.get(r.from_table) ?? parents.set(r.from_table, new Set()).get(r.from_table)!).add(r.to_table);
    linked.add(r.from_table);
    linked.add(r.to_table);
  }
  const rank = new Map<string, number>();
  const visiting = new Set<string>();
  const rankOf = (t: string): number => {
    if (rank.has(t)) return rank.get(t)!;
    if (visiting.has(t)) return 0;
    visiting.add(t);
    let r = 0;
    for (const p of parents.get(t) ?? []) r = Math.max(r, rankOf(p) + 1);
    visiting.delete(t);
    rank.set(t, r);
    return r;
  };
  const columns: Node[][] = [];
  for (const n of nodes) if (linked.has(n.name)) (columns[rankOf(n.name)] ??= []).push(n);
  // order each column by the average position of its neighbours in the previous column
  const neighbours = (t: string) => rels.filter((r) => r.from_table === t || r.to_table === t).map((r) => (r.from_table === t ? r.to_table : r.from_table));
  for (let pass = 0; pass < 3; pass++) {
    for (let c = 1; c < columns.length; c++) {
      const prev = new Map((columns[c - 1] ?? []).map((n, i) => [n.name, i]));
      const score = (n: Node) => {
        const xs = neighbours(n.name).map((x) => prev.get(x)).filter((x): x is number => x !== undefined);
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 1e6;
      };
      columns[c]?.sort((a, b) => score(a) - score(b));
    }
  }
  const isolated = nodes.filter((n) => !linked.has(n.name)).sort((a, b) => a.name.localeCompare(b.name));
  const perCol = Math.max(3, Math.ceil(Math.sqrt(isolated.length)));
  for (let i = 0; i < isolated.length; i += perCol) columns.push(isolated.slice(i, i + perCol));
  const out: Node[] = [];
  let x = 40;
  for (const col of columns) {
    if (!col?.length) continue;
    let y = 40;
    const w = Math.max(...col.map(nodeW));
    for (const n of col) {
      out.push({ ...n, x, y });
      y += nodeH(n, keysOnly) + GAP_Y;
    }
    x += w + GAP_X;
  }
  return out;
}

// ------------------------------------------------------------------ component
export function ErdTab({ tab, visible }: { tab: WorkTab; visible: boolean }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const storeKey = `fbadmin.erd.${sid}.${dbid}`;
  const [nodes, setNodes] = useState<Node[]>([]);
  const [rels, setRels] = useState<Rel[]>([]);
  const [view, setView] = useState({ tx: 0, ty: 0, k: 1 });
  const [selected, setSelected] = useState<string | null>(tab.props?.focus ?? null);
  const [keysOnly, setKeysOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [link, setLink] = useState<{ table: string; col: string; x: number; y: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ kind: "pan" | "node"; sx: number; sy: number; ox: number; oy: number; name?: string } | null>(null);
  // touch: the pointers on the canvas; two of them pinch-zoom around their midpoint
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ d: number; mx: number; my: number; view: { tx: number; ty: number; k: number } } | null>(null);
  const coarse = useCoarsePointer();
  const newSeq = useRef(1);

  const load = async (relayout = false) => {
    setLoading(true);
    setError(null);
    try {
      const d = await schemaApi.erd(sid, dbid);
      let saved: Record<string, { x: number; y: number }> = {};
      try { saved = relayout ? {} : JSON.parse(localStorage.getItem(storeKey) || "{}"); } catch { /* ignore */ }
      const fresh: Node[] = d.tables.map((t) => ({ name: t.name, x: 0, y: 0, columns: t.columns, comment: t.comment, gtt: t.relation_type === 4 || t.relation_type === 5 }));
      const laid = autoLayout(fresh, d.relations, keysOnly);
      const placed = laid.map((n) => (saved[n.name] ? { ...n, ...saved[n.name] } : n));
      // keep tables designed in this session
      setNodes((cur) => [...placed, ...cur.filter((n) => n.isNew && !placed.some((p) => p.name === n.name))]);
      setRels((cur) => [...d.relations, ...cur.filter((r) => r.isNew)]);
      setTimeout(() => fit(placed), 0);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    try {
      const pos: Record<string, { x: number; y: number }> = {};
      for (const n of nodes) if (!n.isNew) pos[n.name] = { x: n.x, y: n.y };
      if (nodes.length) localStorage.setItem(storeKey, JSON.stringify(pos));
    } catch { /* storage unavailable */ }
  }, [nodes]);

  const byName = useMemo(() => new Map(nodes.map((n) => [n.name, n])), [nodes]);

  const bbox = (list: Node[] = nodes) => {
    if (!list.length) return { x: 0, y: 0, w: 100, h: 100 };
    const x0 = Math.min(...list.map((n) => n.x)), y0 = Math.min(...list.map((n) => n.y));
    const x1 = Math.max(...list.map((n) => n.x + nodeW(n))), y1 = Math.max(...list.map((n) => n.y + nodeH(n, keysOnly)));
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  };
  const fit = (list: Node[] = nodes) => {
    const el = svgRef.current;
    if (!el || !list.length) return;
    const r = el.getBoundingClientRect();
    const b = bbox(list);
    const k = Math.min(1.2, Math.max(0.15, Math.min((r.width - 40) / b.w, (r.height - 40) / b.h)));
    setView({ k, tx: (r.width - b.w * k) / 2 - b.x * k, ty: (r.height - b.h * k) / 2 - b.y * k });
  };
  const toWorld = (cx: number, cy: number) => {
    const r = svgRef.current!.getBoundingClientRect();
    return { x: (cx - r.left - view.tx) / view.k, y: (cy - r.top - view.ty) / view.k };
  };

  // ---- pointer handling
  const onWheel = (e: React.WheelEvent) => {
    const r = svgRef.current!.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const k = Math.min(3, Math.max(0.1, view.k * Math.exp(-e.deltaY * 0.0015)));
    setView({ k, tx: px - ((px - view.tx) * k) / view.k, ty: py - ((py - view.ty) * k) / view.k });
  };
  const pinchGeometry = () => {
    const [a, b] = [...pointers.current.values()];
    const r = svgRef.current!.getBoundingClientRect();
    return { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, mx: (a.x + b.x) / 2 - r.left, my: (a.y + b.y) / 2 - r.top };
  };
  // every pointer is tracked (capture phase, before the node and handle handlers decide what to drag)
  const onPointerDownCapture = (e: React.PointerEvent) => {
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    svgRef.current?.setPointerCapture?.(e.pointerId);
    if (pointers.current.size === 2) { // a second finger: pinch instead of dragging
      drag.current = null;
      setLink(null);
      pinch.current = { ...pinchGeometry(), view };
    }
  };
  const endPointer = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  };
  const onBgDown = (e: React.PointerEvent) => {
    if (e.button !== 0 || pointers.current.size > 1) return;
    drag.current = { kind: "pan", sx: e.clientX, sy: e.clientY, ox: view.tx, oy: view.ty };
  };
  const onNodeDown = (e: React.PointerEvent, n: Node) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    setSelected(n.name);
    if (pointers.current.size > 1) return;
    drag.current = { kind: "node", sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y, name: n.name };
  };
  const onMove = (e: React.PointerEvent) => {
    if (pointers.current.has(e.pointerId)) pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const p = pinch.current;
    if (p && pointers.current.size >= 2) {
      const g = pinchGeometry();
      const k = Math.min(3, Math.max(0.1, p.view.k * (g.d / p.d)));
      const wx = (p.mx - p.view.tx) / p.view.k, wy = (p.my - p.view.ty) / p.view.k; // world point under the start midpoint
      setView({ k, tx: g.mx - wx * k, ty: g.my - wy * k });
      return;
    }
    const d = drag.current;
    if (link) {
      const w = toWorld(e.clientX, e.clientY);
      setLink({ ...link, x: w.x, y: w.y });
      return;
    }
    if (!d) return;
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
    if (d.kind === "pan") setView((v) => ({ ...v, tx: d.ox + dx, ty: d.oy + dy }));
    else setNodes((cur) => cur.map((n) => (n.name === d.name ? { ...n, x: d.ox + dx / view.k, y: d.oy + dy / view.k } : n)));
  };
  const onUp = (e: React.PointerEvent) => {
    endPointer(e);
    drag.current = null;
    if (!link) return;
    // the canvas captures the pointer, so look up what is under it
    const el = (document.elementFromPoint(e.clientX, e.clientY) ?? (e.target as Element)).closest("[data-table]");
    setLink(null);
    if (!el) return;
    const parent = el.getAttribute("data-table")!;
    let refCol = el.getAttribute("data-col");
    const p = byName.get(parent);
    if (!p) return;
    if (!refCol) refCol = p.columns.find((c) => c.pk)?.name ?? null;
    if (!refCol || (parent === link.table && refCol === link.col)) return;
    addRelation(link.table, link.col, parent, refCol);
  };
  const startLink = (e: React.PointerEvent, table: string, col: string) => {
    e.stopPropagation();
    if (pointers.current.size > 1) return;
    const w = toWorld(e.clientX, e.clientY);
    setLink({ table, col, x: w.x, y: w.y });
  };

  // ---- design operations
  const addRelation = (child: string, col: string, parent: string, refCol: string) => {
    let name = `FK_${child}_${parent}`.slice(0, 31);
    for (let i = 2; rels.some((r) => r.name === name); i++) name = `FK_${child}_${parent}`.slice(0, 28) + `_${i}`;
    setRels((cur) => [...cur, { name, from_table: child, from_columns: [col], to_table: parent, to_columns: [refCol], isNew: true }]);
    // a column designed here takes the type of the column it references
    const refType = byName.get(parent)?.columns.find((c) => c.name === refCol);
    setNodes((cur) => cur.map((n) => (n.name === child ? {
      ...n, columns: n.columns.map((c) => (c.name === col ? { ...c, fk: true, type: c.isNew && refType ? refType.data_type ?? refType.type : c.type } : c)),
    } : n)));
    app.toast(t("Foreign key {child}.{col} → {parent}.{refCol} added to the design", { child, col, parent, refCol }), "info");
  };
  const addTable = () => {
    let name = `NEW_TABLE_${newSeq.current++}`;
    while (byName.has(name)) name = `NEW_TABLE_${newSeq.current++}`;
    const el = svgRef.current!.getBoundingClientRect();
    const node: Node = { name, x: 0, y: 0, isNew: true, columns: [{ name: "ID", type: "INTEGER", pk: true, not_null: true, isNew: true }] };
    // first free spot, scanning from the top left of the visible area
    const tl = toWorld(el.left + 20, el.top + 20), br = toWorld(el.right - 360, el.bottom - 40);
    const w = nodeW(node) + 30, h = nodeH(node, keysOnly) + 60;
    const free = (x: number, y: number) => !nodes.some((n) => x < n.x + nodeW(n) + 20 && x + w > n.x && y < n.y + nodeH(n, keysOnly) + 20 && y + h > n.y);
    let spot = { x: tl.x, y: tl.y }, found = false;
    for (let y = tl.y; y < Math.max(br.y, tl.y + 1) && !found; y += 40)
      for (let x = tl.x; x < Math.max(br.x, tl.x + 1) && !found; x += 40)
        if (free(x, y)) { spot = { x, y }; found = true; }
    if (!found) { const b = bbox(); spot = { x: b.x + b.w + GAP_X, y: b.y }; }
    setNodes((cur) => [...cur, { ...node, ...spot }]);
    setSelected(name);
  };
  const updateNode = (name: string, fn: (n: Node) => Node) => setNodes((cur) => cur.map((n) => (n.name === name ? fn(n) : n)));
  const renameTable = (old: string, name: string) => {
    name = name.toUpperCase().replace(/[^A-Z0-9_$]/g, "_");
    if (!name || (name !== old && byName.has(name))) return;
    setNodes((cur) => cur.map((n) => (n.name === old ? { ...n, name } : n)));
    setRels((cur) => cur.map((r) => ({ ...r, from_table: r.from_table === old ? name : r.from_table, to_table: r.to_table === old ? name : r.to_table })));
    setSelected(name);
  };
  const removeTable = (name: string) => {
    setNodes((cur) => cur.filter((n) => n.name !== name));
    setRels((cur) => cur.filter((r) => r.from_table !== name && r.to_table !== name));
    setSelected(null);
  };

  const ddl = useMemo(() => {
    const out: string[] = [];
    for (const n of nodes.filter((x) => x.isNew)) {
      const cols = n.columns.map((c) => `    ${quoteIdent(c.name)} ${c.type}${c.not_null || c.pk ? " NOT NULL" : ""}`);
      const pk = n.columns.filter((c) => c.pk).map((c) => quoteIdent(c.name));
      if (pk.length) cols.push(`    CONSTRAINT ${quoteIdent(`PK_${n.name}`.slice(0, 31))} PRIMARY KEY (${pk.join(", ")})`);
      out.push(`CREATE TABLE ${quoteIdent(n.name)} (\n${cols.join(",\n")}\n);`);
    }
    for (const n of nodes.filter((x) => !x.isNew)) {
      for (const c of n.columns.filter((x) => x.isNew))
        out.push(`ALTER TABLE ${quoteIdent(n.name)} ADD ${quoteIdent(c.name)} ${c.type}${c.not_null ? " NOT NULL" : ""};`);
    }
    for (const r of rels.filter((x) => x.isNew)) {
      out.push(`ALTER TABLE ${quoteIdent(r.from_table)} ADD CONSTRAINT ${quoteIdent(r.name)} FOREIGN KEY (${r.from_columns.map(quoteIdent).join(", ")}) ` +
        `REFERENCES ${quoteIdent(r.to_table)} (${r.to_columns.map(quoteIdent).join(", ")});`);
    }
    return out.join("\n\n");
  }, [nodes, rels]);

  // ---- export (see ErdExport.tsx)
  const dbLabel = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? "erd";
  const openExport = () => {
    const el = svgRef.current;
    if (!el) return;
    const v = view, rect = el.getBoundingClientRect();
    const visible = { x: -v.tx / v.k, y: -v.ty / v.k, w: rect.width / v.k, h: rect.height / v.k };
    const all = bbox();
    app.showDialog(<ErdExportDialog dbName={dbLabel} tables={nodes.length}
      build={(o, meta) => buildErdSvg(el, o.scope === "view" ? visible : all, o, meta)} />);
  };

  // ---- geometry of relation lines
  const rowY = (n: Node, col: string) => {
    const cols = visibleCols(n, keysOnly);
    const i = cols.findIndex((c) => c.name === col);
    return n.y + HEAD_H + (i < 0 ? 0 : i) * ROW_H + ROW_H / 2 + 2;
  };
  const relPath = (r: Rel) => {
    const a = byName.get(r.from_table), b = byName.get(r.to_table);
    if (!a || !b) return null;
    const ay = rowY(a, r.from_columns[0]), by = rowY(b, r.to_columns[0]);
    const aw = nodeW(a), bw = nodeW(b);
    if (a === b) {
      const x = a.x + aw;
      return { d: `M ${x} ${ay} C ${x + 50} ${ay}, ${x + 50} ${by}, ${x} ${by}`, ax: x, ay, adir: 1, bx: x, by, bdir: 1 };
    }
    const right = a.x + aw / 2 < b.x + bw / 2;
    const ax = right ? a.x + aw : a.x, bx = right ? b.x : b.x + bw;
    const dx = Math.max(40, Math.abs(bx - ax) / 2) * (right ? 1 : -1);
    return { d: `M ${ax} ${ay} C ${ax + dx} ${ay}, ${bx - dx} ${by}, ${bx} ${by}`, ax, ay, adir: right ? 1 : -1, bx, by, bdir: right ? -1 : 1 };
  };

  const sel = selected ? byName.get(selected) : null;
  const selRels = sel ? rels.filter((r) => r.from_table === sel.name || r.to_table === sel.name) : [];

  return (
    <div className="sch-col" style={{ display: visible ? undefined : "none" }}>
      <div className="toolbar">
        <button className="btn" onClick={() => load()}><IconRefresh /> {t("Reload")}</button>
        <button className="btn" onClick={() => { const laid = autoLayout(nodes, rels, keysOnly); setNodes(laid); setTimeout(() => fit(laid), 0); }}>{t("Auto layout")}</button>
        <button className="btn" onClick={() => fit()}>{t("Fit")}</button>
        <button className="btn icon" title={t("Zoom in")} onClick={() => setView((v) => ({ ...v, k: Math.min(3, v.k * 1.2) }))}>＋</button>
        <button className="btn icon" title={t("Zoom out")} onClick={() => setView((v) => ({ ...v, k: Math.max(0.1, v.k / 1.2) }))}>－</button>
        <span className="muted">{Math.round(view.k * 100)}%</span>
        <label><input type="checkbox" checked={keysOnly} onChange={(e) => setKeysOnly(e.target.checked)} /> {t("Keys only")}</label>
        <span className="sep" />
        <button className="btn" onClick={addTable}><IconPlus /> {t("Add table")}</button>
        <button className="btn" disabled={!ddl} onClick={() => app.openQueryTool(sid, dbid, ddl)}><IconTerminal /> {t("Generate DDL")}</button>
        <span className="grow" />
        <span className="muted">{t("{tables} tables · {relations} relations", { tables: nodes.length, relations: rels.length })}</span>
        <button className="btn" disabled={!nodes.length} onClick={openExport}><IconDownload /> {t("Export image…")}</button>
      </div>
      {error && <div className="pad err">{error}</div>}
      <div className="sch-erd">
        <svg ref={svgRef} className="sch-erd-canvas" onWheel={onWheel} onPointerDownCapture={onPointerDownCapture}
          onPointerDown={onBgDown} onPointerMove={onMove} onPointerUp={onUp}
          onPointerCancel={(e) => { endPointer(e); drag.current = null; setLink(null); }}>
          <g className="erd-root" transform={`translate(${view.tx},${view.ty}) scale(${view.k})`}>
            {rels.map((r) => {
              const p = relPath(r);
              if (!p) return null;
              const hl = selected && (r.from_table === selected || r.to_table === selected);
              const stroke = r.isNew ? "var(--ok)" : hl ? "var(--accent)" : "var(--muted)";
              const f = p.adir * 10;
              return (
                <g key={r.name + r.from_table} data-hl={hl ? (r.isNew ? "var(--ok)" : "var(--muted)") : undefined}>
                  <title>{`${r.name}: ${r.from_table} (${r.from_columns.join(", ")}) → ${r.to_table} (${r.to_columns.join(", ")})`}</title>
                  <path d={p.d} fill="none" stroke={stroke} strokeWidth={hl ? 2 : 1.3} strokeDasharray={r.isNew ? "5 4" : undefined} />
                  {/* many (crow's foot) at the child, one (bar) at the parent */}
                  <path d={`M ${p.ax + f} ${p.ay} L ${p.ax} ${p.ay - 6} M ${p.ax + f} ${p.ay} L ${p.ax} ${p.ay + 6} M ${p.ax + f} ${p.ay} L ${p.ax} ${p.ay}`} stroke={stroke} strokeWidth={1.3} fill="none" />
                  <path d={`M ${p.bx + p.bdir * 7} ${p.by - 6} L ${p.bx + p.bdir * 7} ${p.by + 6}`} stroke={stroke} strokeWidth={1.5} />
                </g>
              );
            })}
            {nodes.map((n) => {
              const w = nodeW(n), h = nodeH(n, keysOnly);
              const cols = visibleCols(n, keysOnly);
              const isSel = n.name === selected;
              return (
                <g key={n.name} transform={`translate(${n.x},${n.y})`} data-table={n.name} className="erd-node">
                  <rect width={w} height={h} rx={6} fill="var(--panel)" data-sel={isSel ? (n.isNew ? "var(--ok)" : "var(--border)") : undefined} stroke={isSel ? "var(--accent)" : n.isNew ? "var(--ok)" : "var(--border)"}
                    strokeWidth={isSel ? 2 : 1} strokeDasharray={n.isNew ? "5 3" : undefined} />
                  <path d={`M 0 6 a 6 6 0 0 1 6 -6 h ${w - 12} a 6 6 0 0 1 6 6 v ${HEAD_H - 6} h ${-w} z`}
                    fill={n.isNew ? "var(--new)" : "var(--accent-soft)"} style={{ cursor: "move" }} onPointerDown={(e) => onNodeDown(e, n)} />
                  <text x={PAD} y={18} fontWeight={700} fontSize={12.5} fill="var(--text)" fontFamily="var(--mono)" style={{ pointerEvents: "none" }}>
                    {n.name}{n.gtt ? "  (GTT)" : ""}
                  </text>
                  {n.comment && <title>{n.comment}</title>}
                  {cols.map((c, i) => {
                    const y = HEAD_H + i * ROW_H;
                    return (
                      <g key={c.name} transform={`translate(0,${y})`} data-table={n.name} data-col={c.name} className="erd-row"
                        onPointerDown={(e) => { e.stopPropagation(); setSelected(n.name); }}>
                        <rect x={1} y={0} width={w - 2} height={ROW_H} fill={c.isNew ? "var(--new)" : "transparent"} />
                        <text x={PAD} y={14} fontSize={11.5} fontFamily="var(--mono)" fill={c.pk ? "var(--warn)" : c.fk ? "var(--accent)" : "var(--text)"}
                          fontWeight={c.pk ? 700 : 400}>
                          {c.pk ? "🔑 " : c.fk ? "↗ " : ""}{c.name}{c.not_null && !c.pk ? " *" : ""}
                        </text>
                        <text x={w - PAD - 12} y={14} fontSize={10.5} fontFamily="var(--mono)" fill="var(--muted)" textAnchor="end">{c.type}</text>
                        <circle data-noexport cx={w - 7} cy={ROW_H / 2} r={4} className="erd-handle"
                          onPointerDown={(e) => startLink(e, n.name, c.name)}>
                          <title>{t("Drag onto the referenced column to create a foreign key")}</title>
                        </circle>
                        {coarse && ( /* a finger-sized, invisible grip around the handle */
                          <circle data-noexport cx={w - 7} cy={ROW_H / 2} r={13} fill="transparent" className="erd-grip"
                            onPointerDown={(e) => startLink(e, n.name, c.name)} />
                        )}
                      </g>
                    );
                  })}
                </g>
              );
            })}
            {link && (() => {
              const n = byName.get(link.table);
              if (!n) return null;
              const sx = n.x + nodeW(n) - 7, sy = rowY(n, link.col);
              return <path data-noexport d={`M ${sx} ${sy} L ${link.x} ${link.y}`} stroke="var(--ok)" strokeWidth={2} strokeDasharray="4 3" fill="none" style={{ pointerEvents: "none" }} />;
            })()}
          </g>
        </svg>
        {loading && <div className="sch-erd-overlay muted">{t("Loading…")}</div>}
        {!loading && nodes.length === 0 && <div className="sch-erd-overlay muted">{t("No tables yet — use “Add table” to design one.")}</div>}
        <div className="sch-erd-help muted">{coarse
          ? t("Pinch: zoom · drag background: pan · drag a header: move · drag a column’s ● onto another column: foreign key")
          : t("Wheel: zoom · drag background: pan · drag a header: move · drag a column’s ● onto another column: foreign key")}</div>
        {sel && (
          <div className="sch-erd-panel">
            <div className="sch-erd-panel-head">
              {sel.isNew ? (
                <input className="text" defaultValue={sel.name} key={sel.name} onBlur={(e) => renameTable(sel.name, e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
              ) : <b>{sel.name}</b>}
              <button className="btn icon small" title={t("Close")} onClick={() => setSelected(null)}>×</button>
            </div>
            {sel.comment && <div className="muted" style={{ marginBottom: 6 }}>{sel.comment}</div>}
            <table className="sch-erd-cols">
              <thead><tr><th>{t("Column")}</th><th>{t("Type")}</th><th title={t("Primary key")}><IconKey size={12} /></th><th title={t("Not null")}>NN</th><th /></tr></thead>
              <tbody>
                {sel.columns.map((c, i) => (
                  <tr key={i} className={c.isNew ? "new" : ""}>
                    {c.isNew ? (
                      <>
                        <td><input className="text" value={c.name} onChange={(e) => updateNode(sel.name, (n) => ({ ...n, columns: n.columns.map((x, j) => (j === i ? { ...x, name: e.target.value.toUpperCase() } : x)) }))} /></td>
                        <td><input className="text" list="erd-types" value={c.type} onChange={(e) => updateNode(sel.name, (n) => ({ ...n, columns: n.columns.map((x, j) => (j === i ? { ...x, type: e.target.value.toUpperCase() } : x)) }))} /></td>
                        <td><input type="checkbox" disabled={!sel.isNew} checked={!!c.pk} onChange={(e) => updateNode(sel.name, (n) => ({ ...n, columns: n.columns.map((x, j) => (j === i ? { ...x, pk: e.target.checked } : x)) }))} /></td>
                        <td><input type="checkbox" checked={!!c.not_null || !!c.pk} onChange={(e) => updateNode(sel.name, (n) => ({ ...n, columns: n.columns.map((x, j) => (j === i ? { ...x, not_null: e.target.checked } : x)) }))} /></td>
                        <td><button className="btn icon small" title={t("Remove column")} onClick={() => updateNode(sel.name, (n) => ({ ...n, columns: n.columns.filter((_, j) => j !== i) }))}><IconTrash size={12} /></button></td>
                      </>
                    ) : (
                      <>
                        <td>{c.name}</td><td className="muted">{c.type}</td><td>{c.pk ? "✓" : ""}</td><td>{c.not_null ? "✓" : ""}</td><td />
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
            <datalist id="erd-types">{TYPES.map((t) => <option key={t} value={t} />)}</datalist>
            <div style={{ display: "flex", gap: 6, margin: "8px 0" }}>
              <button className="btn small" onClick={() => updateNode(sel.name, (n) => ({ ...n, columns: [...n.columns, { name: `COL${n.columns.length + 1}`, type: "VARCHAR(50)", isNew: true }] }))}><IconPlus size={12} /> {t("Column")}</button>
              {sel.isNew && <button className="btn small danger" onClick={() => removeTable(sel.name)}><IconTrash size={12} /> {t("Remove table")}</button>}
            </div>
            {selRels.length > 0 && <div className="sch-group" style={{ padding: "4px 0" }}>{t("Foreign keys")}</div>}
            {selRels.map((r) => (
              <div key={r.name + r.from_table} className="sch-erd-rel">
                <span>{r.from_table}({r.from_columns.join(", ")}) → {r.to_table}({r.to_columns.join(", ")})</span>
                {r.isNew && <button className="btn icon small" title={t("Remove")} onClick={() => setRels((cur) => cur.filter((x) => x !== r))}><IconTrash size={12} /></button>}
              </div>
            ))}
            {ddl && <div className="muted" style={{ marginTop: 8 }}>{t("Unsaved design changes: use")} <b>{t("Generate DDL")}</b> {t("to review and run them in the Query Tool.")}</div>}
          </div>
        )}
      </div>
    </div>
  );
}

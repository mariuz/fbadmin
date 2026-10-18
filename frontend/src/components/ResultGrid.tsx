import { useMemo, useRef, useState } from "react";
import type { ColumnInfo } from "../types";
import { buildXlsx } from "../lib/xlsx";
import { getPrefs } from "../prefs";
import { IconCopy, IconDownload, IconFilter, IconSpinner, IconX } from "./Icons";

const ROW_H = 25;
const NUMERIC = /INT|NUMERIC|DOUBLE|FLOAT|DECFLOAT|SMALLINT|BIGINT/;

/** Text shown in grid cells: honours the date format preference. */
export function displayValue(v: unknown, type: string): string {
  const s = cellText(v);
  if (getPrefs().dateFormat === "locale" && typeof v === "string" && /TIMESTAMP|DATE|TIME/.test(type)) {
    const d = /^\d{4}-\d{2}-\d{2}/.test(s) ? new Date(s.length === 10 ? s + "T00:00:00" : s) : null;
    if (d && !isNaN(d.getTime())) return type.startsWith("DATE") ? d.toLocaleDateString() : d.toLocaleString();
  }
  return s.replace("T", /TIMESTAMP/.test(type) ? " " : "T");
}

export function cellText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

function csvEscape(v: unknown, sep: string): string {
  if (v === null || v === undefined) return "";
  const s = cellText(v);
  return s.includes(sep) || s.includes('"') || s.includes("\n") ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toDelimited(cols: ColumnInfo[], rows: unknown[][], sep = ","): string {
  return [cols.map((c) => csvEscape(c.name, sep)).join(sep), ...rows.map((r) => r.map((v) => csvEscape(v, sep)).join(sep))].join("\n");
}

const quoteIdent = (n: string) => (/^[A-Z][A-Z0-9_$]*$/.test(n) ? n : `"${n.replace(/"/g, '""')}"`);

function sqlLiteral(v: unknown, type: string): string {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  const s = String(v);
  if (NUMERIC.test(type) && /^-?\d+(\.\d+)?(e[-+]?\d+)?$/i.test(s)) return s;
  if (/^0x[0-9A-F]+$/.test(s) && /BLOB|CHAR/.test(type)) return `x'${s.slice(2)}'`;
  if (/TIMESTAMP|DATE|TIME/.test(type)) return `'${s.replace("T", " ")}'`;
  return `'${s.replace(/'/g, "''")}'`;
}

export function toInserts(table: string, cols: ColumnInfo[], rows: unknown[][]): string {
  const names = cols.map((c) => quoteIdent(c.name)).join(", ");
  return rows
    .map((r) => `INSERT INTO ${quoteIdent(table)} (${names}) VALUES (${r.map((v, i) => sqlLiteral(v, cols[i].type)).join(", ")});`)
    .join("\n") + "\n";
}

export function toMarkdown(cols: ColumnInfo[], rows: unknown[][]): string {
  const esc = (v: unknown) => (v === null || v === undefined ? "NULL" : cellText(v).replace(/\|/g, "\\|").replace(/\n/g, " "));
  return [
    `| ${cols.map((c) => esc(c.name)).join(" | ")} |`,
    `| ${cols.map((c) => (NUMERIC.test(c.type) ? "---:" : "---")).join(" | ")} |`,
    ...rows.map((r) => `| ${r.map(esc).join(" | ")} |`),
  ].join("\n") + "\n";
}

export function download(name: string, text: string | Uint8Array<ArrayBuffer>, type = "text/csv") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ------------------------------------------------------------ cell viewer
const MAGIC: Array<[string, string]> = [
  ["89504E47", "image/png"], ["FFD8FF", "image/jpeg"], ["47494638", "image/gif"], ["52494646", "image/webp"],
  ["25504446", "application/pdf"],
];

function hexToBytes(hex: string): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(new ArrayBuffer(hex.length / 2));
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

function hexDump(bytes: Uint8Array, max = 4096): string {
  const lines: string[] = [];
  for (let off = 0; off < Math.min(bytes.length, max); off += 16) {
    const chunk = Array.from(bytes.slice(off, off + 16));
    const hex = chunk.map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(48);
    const asc = chunk.map((b) => (b >= 32 && b < 127 ? String.fromCharCode(b) : ".")).join("");
    lines.push(`${off.toString(16).padStart(8, "0")}  ${hex} ${asc}`);
  }
  if (bytes.length > max) lines.push(`… ${bytes.length - max} more bytes`);
  return lines.join("\n");
}

export function CellViewer(props: { column: ColumnInfo; value: unknown; onClose: () => void }) {
  const { value, column } = props;
  const text = cellText(value);
  const isHex = typeof value === "string" && /^0x[0-9A-F]*$/.test(value) && value.length % 2 === 0;
  const truncated = typeof value === "string" && value.startsWith("<BINARY");
  const bytes = useMemo(() => (isHex ? hexToBytes((value as string).slice(2)) : null), [value]);
  const mime = isHex ? MAGIC.find(([m]) => (value as string).slice(2).startsWith(m))?.[1] : undefined;
  const json = useMemo(() => {
    if (typeof value !== "string" || !/^\s*[[{]/.test(value)) return null;
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return null; }
  }, [value]);
  const [mode, setMode] = useState<"text" | "json" | "hex" | "image">(mime?.startsWith("image/") ? "image" : json ? "json" : isHex ? "hex" : "text");
  const imgUrl = useMemo(() => (bytes && mime ? URL.createObjectURL(new Blob([bytes], { type: mime })) : null), [bytes, mime]);

  return (
    <div className="cell-viewer">
      <div className="toolbar">
        <b>{column.name}</b> <span className="muted">{column.type.toLowerCase()}</span>
        {value === null || value === undefined ? <span className="muted">NULL</span> : (
          <span className="muted">{bytes ? `${bytes.length.toLocaleString()} bytes` : `${text.length.toLocaleString()} chars`}</span>
        )}
        <span className="grow" />
        <div className="seg">
          <button className={mode === "text" ? "on" : ""} onClick={() => setMode("text")}>Text</button>
          {json && <button className={mode === "json" ? "on" : ""} onClick={() => setMode("json")}>JSON</button>}
          {bytes && <button className={mode === "hex" ? "on" : ""} onClick={() => setMode("hex")}>Hex</button>}
          {imgUrl && <button className={mode === "image" ? "on" : ""} onClick={() => setMode("image")}>Image</button>}
        </div>
        <button className="btn icon small" title="Copy value" onClick={() => navigator.clipboard?.writeText(text)}><IconCopy size={13} /></button>
        {bytes && <button className="btn icon small" title="Download" onClick={() => {
          const url = URL.createObjectURL(new Blob([bytes], { type: mime ?? "application/octet-stream" }));
          const a = document.createElement("a"); a.href = url; a.download = `${column.name}.bin`; a.click();
        }}><IconDownload size={13} /></button>}
        <button className="btn icon small" title="Close" onClick={props.onClose}><IconX size={13} /></button>
      </div>
      <div className="cell-body">
        {truncated && <div className="warn" style={{ padding: "6px 10px" }}>Value is larger than the preview limit; only the start is shown.</div>}
        {mode === "image" && imgUrl ? <img src={imgUrl} alt={column.name} style={{ maxWidth: "100%", padding: 10 }} /> :
          <pre className="mono">{mode === "json" ? json : mode === "hex" && bytes ? hexDump(bytes) : value === null || value === undefined ? "NULL" : text}</pre>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- grid
/** Read-only, virtualized result grid. */
export function ResultGrid(props: {
  columns: ColumnInfo[];
  rows: unknown[][];
  name?: string;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
}) {
  const { columns, rows } = props;
  const scroller = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(600);
  const [sel, setSel] = useState<[number, number] | null>(null);
  const [sort, setSort] = useState<{ col: number; desc: boolean } | null>(null);
  const [filter, setFilter] = useState("");
  const [showFilter, setShowFilter] = useState(false);
  const [viewer, setViewer] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [widths, setWidths] = useState<Record<number, number>>({});
  const [freeze, setFreeze] = useState(false);

  const startResize = (e: React.MouseEvent, ci: number) => {
    e.preventDefault();
    e.stopPropagation();
    const th = (e.target as HTMLElement).parentElement as HTMLElement;
    const x0 = e.clientX, w0 = th.getBoundingClientRect().width;
    const move = (ev: MouseEvent) => setWidths((w) => ({ ...w, [ci]: Math.max(40, w0 + ev.clientX - x0) }));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  const colStyle = (ci: number): React.CSSProperties | undefined =>
    widths[ci] ? { width: widths[ci], minWidth: widths[ci], maxWidth: widths[ci] } : undefined;

  const sorted = useMemo(() => {
    let out = rows.map((r, i) => ({ r, i }));
    const f = filter.trim().toLowerCase();
    if (f) out = out.filter(({ r }) => r.some((v) => v !== null && v !== undefined && cellText(v).toLowerCase().includes(f)));
    if (!sort) return out;
    const numeric = NUMERIC.test(columns[sort.col]?.type ?? "");
    out.sort((a, b) => {
      const x = a.r[sort.col], y = b.r[sort.col];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      const c = numeric ? Number(x) - Number(y) : String(x).localeCompare(String(y));
      return sort.desc ? -c : c;
    });
    return out;
  }, [rows, sort, columns, filter]);

  const first = Math.max(0, Math.floor(scrollTop / ROW_H) - 10);
  const count = Math.ceil(height / ROW_H) + 20;
  const visible = sorted.slice(first, first + count);
  const shownRows = () => sorted.map((x) => x.r);
  const base = props.name ?? "result";
  const exports: Array<[string, () => void]> = [
    ["CSV", () => download(`${base}.csv`, toDelimited(columns, shownRows()))],
    ["TSV", () => download(`${base}.tsv`, toDelimited(columns, shownRows(), "\t"), "text/tab-separated-values")],
    ["JSON", () => download(`${base}.json`, JSON.stringify(shownRows().map((r) => Object.fromEntries(columns.map((c, i) => [c.name, r[i]]))), null, 2), "application/json")],
    ["SQL INSERT", () => download(`${base}.sql`, toInserts(props.name && /^[A-Z_][A-Z0-9_$]*$/i.test(props.name) ? props.name : "TABLE_NAME", columns, shownRows()), "text/sql")],
    ["Markdown", () => download(`${base}.md`, toMarkdown(columns, shownRows()), "text/markdown")],
    ["Excel (XLSX)", () => download(`${base}.xlsx`, buildXlsx(columns, shownRows(), base) as Uint8Array<ArrayBuffer>, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <span className="muted">
          {filter ? `${sorted.length.toLocaleString()} of ` : ""}{rows.length.toLocaleString()}{props.hasMore ? "+" : ""} rows
        </span>
        {props.hasMore && (
          <button className="btn small" disabled={props.loadingMore} onClick={props.onLoadMore} title="Fetch the next rows from the open cursor">
            {props.loadingMore ? <IconSpinner size={13} className="spin" /> : null} Load more
          </button>
        )}
        <span className="grow" />
        <button className={"btn small" + (showFilter ? " on" : "")} onClick={() => setShowFilter(!showFilter)} title="Filter rows"><IconFilter size={13} /></button>
        {showFilter && <input className="text" autoFocus placeholder="filter rows…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 180, padding: "2px 6px" }} />}
        <button className={"btn small" + (freeze ? " on" : "")} onClick={() => setFreeze(!freeze)} title="Keep the first column visible while scrolling sideways">Freeze 1st</button>
        <button className={"btn small" + (viewer ? " on" : "")} onClick={() => setViewer(!viewer)} title="Show the selected cell in full (text, JSON, hex, image)">Cell viewer</button>
        <button className="btn small" title="Copy rows as TSV (paste into a spreadsheet)" onClick={() => navigator.clipboard?.writeText(toDelimited(columns, shownRows(), "\t"))}><IconCopy size={13} /> Copy</button>
        <div style={{ position: "relative" }}>
          <button className="btn small" onClick={() => setExportOpen(!exportOpen)}><IconDownload size={13} /> Export ▾</button>
          {exportOpen && (
            <div className="ctx" style={{ position: "absolute", right: 0, top: "100%", minWidth: 150 }} onMouseLeave={() => setExportOpen(false)}>
              {exports.map(([label, fn]) => <button key={label} onClick={() => { setExportOpen(false); fn(); }}>{label}</button>)}
            </div>
          )}
        </div>
      </div>
      <div style={{ flex: 1, minHeight: 0, display: "flex" }}>
        <div
          ref={(el) => {
            scroller.current = el;
            if (el && el.clientHeight && el.clientHeight !== height) setHeight(el.clientHeight);
          }}
          style={{ flex: 1, overflow: "auto", outline: "none", minWidth: 0 }}
          tabIndex={0}
          onScroll={(e) => {
            const el = e.target as HTMLDivElement;
            setScrollTop(el.scrollTop);
          }}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "c" && sel) navigator.clipboard?.writeText(cellText(sorted[sel[0]]?.r[sel[1]]));
            if (!sel) return;
            const [r, c] = sel;
            const mv: Record<string, [number, number]> = { ArrowDown: [r + 1, c], ArrowUp: [r - 1, c], ArrowLeft: [r, c - 1], ArrowRight: [r, c + 1] };
            if (mv[e.key]) {
              e.preventDefault();
              const [nr, nc] = mv[e.key];
              if (nr >= 0 && nr < sorted.length && nc >= 0 && nc < columns.length) setSel([nr, nc]);
            }
          }}
        >
          <table className={"grid" + (freeze ? " freeze1" : "")}>
            <thead>
              <tr>
                <th className="rn">#</th>
                {columns.map((c, i) => (
                  <th key={i} style={{ cursor: "pointer", position: "sticky", ...colStyle(i) }} title="Click to sort, drag the right edge to resize"
                    onClick={() => setSort(sort?.col === i ? (sort.desc ? null : { col: i, desc: true }) : { col: i, desc: false })}>
                    {c.name} {sort?.col === i ? (sort.desc ? "▼" : "▲") : ""}
                    <span className="ty">{c.type.toLowerCase()}</span>
                    <span className="col-resizer" onMouseDown={(e) => startResize(e, i)} onClick={(e) => e.stopPropagation()}
                      onDoubleClick={(e) => { e.stopPropagation(); setWidths((w) => { const n = { ...w }; delete n[i]; return n; }); }} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {first > 0 && <tr style={{ height: first * ROW_H }} />}
              {visible.map(({ r, i: orig }, k) => {
                const ri = first + k;
                return (
                  <tr key={orig} style={{ height: ROW_H }}>
                    <td className="rn">{ri + 1}</td>
                    {r.map((v, ci) => {
                      const isNull = v === null || v === undefined;
                      const cls = [isNull ? "null" : "", NUMERIC.test(columns[ci]?.type ?? "") ? "num" : "", sel && sel[0] === ri && sel[1] === ci ? "sel" : ""].join(" ");
                      return (
                        <td key={ci} className={cls} style={colStyle(ci)} title={isNull ? "NULL" : cellText(v).slice(0, 500)}
                          onClick={() => setSel([ri, ci])} onDoubleClick={() => { setSel([ri, ci]); setViewer(true); }}>
                          {isNull ? getPrefs().nullDisplay : displayValue(v, columns[ci]?.type ?? "").slice(0, 1000)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
              {first + count < sorted.length && <tr style={{ height: (sorted.length - first - count) * ROW_H }} />}
            </tbody>
          </table>
        </div>
        {viewer && (
          <div style={{ width: "38%", minWidth: 260, borderLeft: "1px solid var(--border)", display: "flex" }}>
            {sel && sorted[sel[0]] ? (
              <CellViewer key={`${sel[0]}:${sel[1]}`} column={columns[sel[1]]} value={sorted[sel[0]].r[sel[1]]} onClose={() => setViewer(false)} />
            ) : <div className="empty-state">Select a cell (double-click opens this panel).</div>}
          </div>
        )}
      </div>
    </div>
  );
}

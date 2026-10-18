import { useMemo, useRef, useState } from "react";
import type { ColumnInfo } from "../types";
import { IconCopy, IconDownload } from "./Icons";

const ROW_H = 25;
const NUMERIC = /INT|NUMERIC|DOUBLE|FLOAT|DECFLOAT|SMALLINT|BIGINT/;

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

export function download(name: string, text: string, type = "text/csv") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Read-only, virtualized result grid. */
export function ResultGrid(props: { columns: ColumnInfo[]; rows: unknown[][]; name?: string }) {
  const { columns, rows } = props;
  const scroller = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(600);
  const [sel, setSel] = useState<[number, number] | null>(null);
  const [sort, setSort] = useState<{ col: number; desc: boolean } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows.map((r, i) => ({ r, i }));
    const numeric = NUMERIC.test(columns[sort.col]?.type ?? "");
    const out = rows.map((r, i) => ({ r, i }));
    out.sort((a, b) => {
      const x = a.r[sort.col], y = b.r[sort.col];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      const c = numeric ? Number(x) - Number(y) : String(x).localeCompare(String(y));
      return sort.desc ? -c : c;
    });
    return out;
  }, [rows, sort, columns]);

  const first = Math.max(0, Math.floor(scrollTop / ROW_H) - 10);
  const count = Math.ceil(height / ROW_H) + 20;
  const visible = sorted.slice(first, first + count);

  const copySel = () => {
    if (!sel) return;
    navigator.clipboard?.writeText(cellText(sorted[sel[0]]?.r[sel[1]]));
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <span className="muted">{rows.length.toLocaleString()} rows</span>
        <span className="grow" />
        <button className="btn small" title="Copy all rows as TSV (paste into a spreadsheet)" onClick={() => navigator.clipboard?.writeText(toDelimited(columns, rows, "\t"))}><IconCopy size={13} /> Copy</button>
        <button className="btn small" onClick={() => download(`${props.name ?? "result"}.csv`, toDelimited(columns, rows))}><IconDownload size={13} /> CSV</button>
        <button className="btn small" onClick={() => download(`${props.name ?? "result"}.json`, JSON.stringify(rows.map((r) => Object.fromEntries(columns.map((c, i) => [c.name, r[i]]))), null, 2), "application/json")}><IconDownload size={13} /> JSON</button>
      </div>
      <div
        ref={(el) => {
          (scroller as any).current = el;
          if (el && el.clientHeight && el.clientHeight !== height) setHeight(el.clientHeight);
        }}
        style={{ flex: 1, overflow: "auto", outline: "none" }}
        tabIndex={0}
        onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "c") copySel();
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
        <table className="grid">
          <thead>
            <tr>
              <th className="rn">#</th>
              {columns.map((c, i) => (
                <th key={i} style={{ cursor: "pointer" }} title="Click to sort" onClick={() => setSort(sort?.col === i ? (sort.desc ? null : { col: i, desc: true }) : { col: i, desc: false })}>
                  {c.name} {sort?.col === i ? (sort.desc ? "▼" : "▲") : ""}
                  <span className="ty">{c.type.toLowerCase()}</span>
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
                      <td key={ci} className={cls} title={isNull ? "NULL" : cellText(v).slice(0, 2000)} onClick={() => setSel([ri, ci])}>
                        {isNull ? "null" : cellText(v)}
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
    </div>
  );
}

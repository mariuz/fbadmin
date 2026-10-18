import { useEffect, useState } from "react";
import { api } from "../api";
import { errMsg, useApp } from "../context";
import { cellText, displayValue, download, toDelimited } from "./ResultGrid";
import { getPrefs } from "../prefs";
import { t } from "../i18n";
import { Modal } from "./Modal";
import { IconDownload, IconFilter, IconPlus, IconRefresh, IconSave, IconTrash } from "./Icons";

type Row = { values: unknown[]; orig?: unknown[]; status: "clean" | "changed" | "new" | "deleted" };
const NUMERIC = /INT|NUMERIC|DOUBLE|FLOAT|DECFLOAT/;
const KEY_COL = "FBADMIN$DB_KEY";

export function DataView(props: { serverId: string; dbId: string; otype: string; name: string; showFilter?: boolean }) {
  const app = useApp();
  const [data, setData] = useState<Awaited<ReturnType<typeof api.tableData>> | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [limit, setLimit] = useState(100);
  const [offset, setOffset] = useState(0);
  const [where, setWhere] = useState("");
  const [whereInput, setWhereInput] = useState("");
  const [showFilter, setShowFilter] = useState(!!props.showFilter);
  const [order, setOrder] = useState<{ col: string; desc: boolean } | null>(null);
  const [edit, setEdit] = useState<[number, number] | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const editable = props.otype === "tables";
  const [big, setBig] = useState<{ ri: number; ci: number; name: string; blob: boolean; text: boolean } | null>(null);

  const load = async (off = offset) => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.tableData(props.serverId, props.dbId, props.name, {
        limit, offset: off, ...(where ? { where } : {}), ...(order ? { order_by: order.col, descending: order.desc } : {}),
      });
      setData(d);
      setRows(d.rows.map((r) => ({ values: [...r], orig: r, status: "clean" })));
      setOffset(off);
      setSel(null);
      setEdit(null);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(0); }, [limit, where, order]);

  const cols = data?.columns ?? [];
  const keyCols = data ? (data.primary_key.length ? data.primary_key : [KEY_COL]) : [];
  const meta = (name: string) => data?.column_meta.find((c) => c.name === name);
  const visibleCols = cols.map((c, i) => ({ c, i })).filter(({ c }) => c.name !== KEY_COL);
  const dirty = rows.some((r) => r.status !== "clean");

  const setCell = (ri: number, ci: number, v: unknown) => {
    setRows((rs) => rs.map((r, i) => {
      if (i !== ri) return r;
      const values = [...r.values];
      values[ci] = v;
      return { ...r, values, status: r.status === "new" ? "new" : "changed" };
    }));
  };

  const save = async () => {
    if (!data) return;
    const idx = (n: string) => cols.findIndex((c) => c.name === n);
    const keyOf = (r: Row) => Object.fromEntries(keyCols.map((k) => [k, r.orig![idx(k)]]));
    const changes = {
      deletes: rows.filter((r) => r.status === "deleted" && r.orig).map((r) => ({ key: keyOf(r) })),
      updates: rows.filter((r) => r.status === "changed").map((r) => ({
        key: keyOf(r),
        values: Object.fromEntries(cols.map((c, i) => [c.name, r.values[i]]).filter(([n], i) => n !== KEY_COL && r.values[i] !== r.orig![i])),
      })),
      inserts: rows.filter((r) => r.status === "new").map((r) => ({
        values: Object.fromEntries(cols.map((c, i) => [c.name, r.values[i]]).filter(([n, v]) => n !== KEY_COL && v !== undefined)),
      })),
    };
    try {
      const res = await api.saveTableData(props.serverId, props.dbId, props.name, changes);
      app.toast(t("Saved: {statements} statement(s), {affected} row(s)", { statements: res.statements, affected: res.affected }));
      load(offset);
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
  };

  const addRow = () => {
    const values = cols.map(() => undefined as unknown);
    setRows((rs) => [...rs, { values, status: "new" }]);
    const first = visibleCols.find(({ c }) => !meta(c.name)?.computed && !meta(c.name)?.identity);
    if (first) setEdit([rows.length, first.i]);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn icon" title={t("Refresh")} onClick={() => load(offset)}><IconRefresh /></button>
        <button className={"btn icon" + (showFilter ? " on" : "")} title={t("Filter")} onClick={() => setShowFilter(!showFilter)}><IconFilter /></button>
        {editable && (
          <>
            <span className="sep" />
            <button className="btn" onClick={addRow}><IconPlus /> {t("Row")}</button>
            <button className="btn" disabled={sel === null} onClick={() => {
              if (sel === null) return;
              setRows((rs) => rs.flatMap((r, i) => (i !== sel ? [r] : r.status === "new" ? [] : [{ ...r, status: "deleted" as const }])));
            }}><IconTrash /> {t("Delete")}</button>
            <button className="btn primary" disabled={!dirty} onClick={save}><IconSave /> {t("Save")}</button>
            <button className="btn" disabled={!dirty} onClick={() => load(offset)}>{t("Discard")}</button>
          </>
        )}
        <span className="sep" />
        <button className="btn" disabled={offset === 0 || loading} onClick={() => load(Math.max(0, offset - limit))}>‹ {t("Prev")}</button>
        <span className="muted">{t("rows {from}–{to}", { from: rows.length ? offset + 1 : 0, to: offset + (data?.rows.length ?? 0) })}</span>
        <button className="btn" disabled={!data?.has_more || loading} onClick={() => load(offset + limit)}>{t("Next")} ›</button>
        <select className="text" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          {[50, 100, 500, 1000, 5000].map((n) => <option key={n} value={n}>{t("{count} / page", { count: n })}</option>)}
        </select>
        <span className="grow" />
        {data && <button className="btn small" onClick={() => download(`${props.name}.csv`, toDelimited(visibleCols.map(({ c }) => c), rows.map((r) => visibleCols.map(({ i }) => r.values[i]))))}><IconDownload size={13} /> CSV</button>}
      </div>
      {showFilter && (
        <div className="toolbar">
          <span className="muted">WHERE</span>
          <input className="text mono" style={{ flex: 1 }} value={whereInput} placeholder={t("e.g. NAME LIKE 'A%' AND ID > 10")} onChange={(e) => setWhereInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && setWhere(whereInput)} autoFocus />
          <button className="btn" onClick={() => setWhere(whereInput)}>{t("Apply")}</button>
          <button className="btn" onClick={() => { setWhereInput(""); setWhere(""); }}>{t("Clear")}</button>
        </div>
      )}
      {error && <div className="pad err" style={{ whiteSpace: "pre-wrap" }}>{error}</div>}
      {!editable && <div className="pad muted" style={{ padding: "4px 10px" }}>{props.otype === "views" ? t("Read-only (view).") : t("Read-only (system table).")}</div>}
      {editable && data && !data.primary_key.length && <div className="pad warn" style={{ padding: "4px 10px" }}>{t("No primary key: rows are identified by RDB$DB_KEY (valid only within this session's view of the data).")}</div>}
      <div style={{ flex: 1, overflow: "auto" }}>
        <table className="grid">
          <thead>
            <tr>
              <th className="rn">#</th>
              {visibleCols.map(({ c }) => {
                const m = meta(c.name);
                return (
                  <th key={c.name} style={{ cursor: "pointer" }} onClick={() => setOrder(order?.col === c.name ? (order.desc ? null : { col: c.name, desc: true }) : { col: c.name, desc: false })}>
                    {m?.primary_key ? "🔑 " : ""}{c.name} {order?.col === c.name ? (order.desc ? "▼" : "▲") : ""}
                    <span className="ty">{(m?.data_type ?? c.type).toLowerCase()}{m?.not_null ? " not null" : ""}</span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className={r.status === "clean" ? "" : r.status} onClick={() => setSel(ri)}>
                <td className="rn" style={sel === ri ? { color: "var(--accent)", fontWeight: 700 } : undefined}>{offset + ri + 1}</td>
                {visibleCols.map(({ c, i }) => {
                  const v = r.values[i];
                  const m = meta(c.name);
                  const readOnly = !editable || r.status === "deleted" || !!m?.computed || (m?.identity === "ALWAYS");
                  const isEditing = edit && edit[0] === ri && edit[1] === i;
                  const changed = r.orig && r.values[i] !== r.orig[i];
                  const isNull = v === null;
                  const cls = [isNull ? "null" : "", NUMERIC.test(c.type) ? "num" : "", changed ? "dirty" : ""].join(" ");
                  if (isEditing) {
                    return (
                      <td key={c.name} className="sel">
                        <div style={{ display: "flex", gap: 4 }}>
                          <input
                            autoFocus
                            defaultValue={v === undefined || v === null ? "" : cellText(v)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") { setCell(ri, i, (e.target as HTMLInputElement).value); setEdit(null); }
                              if (e.key === "Escape") setEdit(null);
                            }}
                            onBlur={(e) => { if (e.target.value !== (v === undefined || v === null ? "" : cellText(v))) setCell(ri, i, e.target.value); setEdit(null); }}
                          />
                          <button className="btn small" title={t("Set NULL")} onMouseDown={(e) => { e.preventDefault(); setCell(ri, i, null); setEdit(null); }}>∅</button>
                          <button className="btn small" title={t("Edit in a larger editor / upload a file")} onMouseDown={(e) => { e.preventDefault(); setEdit(null); setBig({ ri, ci: i, name: c.name, blob: m?.field_type === 261, text: m?.field_sub_type === 1 }); }}>…</button>
                        </div>
                      </td>
                    );
                  }
                  return (
                    <td key={c.name} className={cls} onDoubleClick={() => !readOnly && setEdit([ri, i])} title={readOnly ? undefined : t("Double-click to edit")}>
                      {v === undefined ? <span className="muted">{m?.identity || m?.default ? "default" : ""}</span>
                        : isNull ? getPrefs().nullDisplay
                        : isBinary(v) ? <span className="muted">{blobLabel(v as string)}</span>
                        : displayValue(v, c.type).slice(0, 500)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        {data && rows.length === 0 && <div className="pad muted">{t("No rows.")}</div>}
      </div>
      {data && <div className="status"><span className="mono" title={data.sql}>{data.sql.length > 160 ? data.sql.slice(0, 160) + "…" : data.sql}</span></div>}
      {big && (
        <BigEditor
          title={big.name}
          value={rows[big.ri]?.values[big.ci]}
          binary={big.blob && !big.text}
          onClose={() => setBig(null)}
          onSave={(v) => { setCell(big.ri, big.ci, v); setBig(null); }}
        />
      )}
    </div>
  );
}

const isBinary = (v: unknown) => typeof v === "string" && (/^0x[0-9A-F]+$/.test(v) || v.startsWith("<BINARY"));
const blobLabel = (v: string) => (v.startsWith("<BINARY") ? v.split(">")[0] + ">" : `<${(v.length - 2) / 2} bytes>`);

/** Multi-line text editor, and file upload/download for BLOB columns. */
function BigEditor(props: {
  title: string; value: unknown; binary: boolean; onClose: () => void; onSave: (v: unknown) => void;
}) {
  const initial = props.value === null || props.value === undefined ? "" : typeof props.value === "object" ? "" : cellText(props.value);
  const [text, setText] = useState(props.binary ? "" : initial);
  const [file, setFile] = useState<{ name: string; hex: string; size: number } | null>(null);
  const pick = () => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.onchange = async () => {
      const f = inp.files?.[0];
      if (!f) return;
      const bytes = new Uint8Array(await f.arrayBuffer());
      let hex = "";
      for (const b of bytes) hex += b.toString(16).padStart(2, "0");
      if (props.binary) setFile({ name: f.name, hex, size: bytes.length });
      else setText(new TextDecoder().decode(bytes));
    };
    inp.click();
  };
  const hexValue = typeof props.value === "string" && /^0x[0-9A-F]+$/.test(props.value) ? props.value.slice(2) : null;
  return (
    <Modal wide title={t("Edit {name}", { name: props.title })} onClose={props.onClose} footer={<>
      <button className="btn" onClick={pick} style={{ marginRight: "auto" }}>{t("Load from file…")}</button>
      {hexValue && <button className="btn" onClick={() => {
        const bytes = new Uint8Array(new ArrayBuffer(hexValue.length / 2));
        for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hexValue.substr(i * 2, 2), 16);
        const url = URL.createObjectURL(new Blob([bytes]));
        const a = document.createElement("a"); a.href = url; a.download = props.title + ".bin"; a.click();
      }}>{t("Download current")}</button>}
      <button className="btn" onClick={() => props.onSave(null)}>{t("Set NULL")}</button>
      <button className="btn" onClick={props.onClose}>{t("Cancel")}</button>
      <button className="btn primary" disabled={props.binary && !file}
        onClick={() => props.onSave(props.binary ? { $hex: file!.hex } : text)}>{t("OK")}</button>
    </>}>
      {props.binary ? (
        <div className="pad">
          {file ? <p>{t("Selected")} <b>{file.name}</b> {t("({count} bytes). Click OK, then Save in the grid.", { count: file.size.toLocaleString() })}</p>
            : <p className="muted">{hexValue
              ? t("Binary BLOB: load a file to replace the value (current: {count} bytes).", { count: hexValue.length / 2 })
              : t("Binary BLOB: load a file to replace the value.")}</p>}
        </div>
      ) : (
        <textarea className="text mono" style={{ width: "100%", height: "50vh" }} value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      )}
    </Modal>
  );
}

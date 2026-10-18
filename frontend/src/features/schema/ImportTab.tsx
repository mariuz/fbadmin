import { useEffect, useState } from "react";
import { api } from "../../api";
import { errMsg, keys, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { IconPlay, IconSpinner, IconTable, IconUpload } from "../../components/Icons";
import { cellText } from "../../components/ResultGrid";
import { schemaApi, uploadFile, type Preview } from "./api";
import { JobStatus, useJob } from "./common";
import { t } from "../../i18n";

interface MapRow { source: string; include: boolean; target: string; type: string }

const norm = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

export function ImportTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [format, setFormat] = useState<string>("");
  const [opts, setOpts] = useState<{ delimiter: string; header: boolean; encoding: string; sheet: string; empty_as_null: boolean }>(
    { delimiter: "", header: true, encoding: "utf-8-sig", sheet: "", empty_as_null: true });
  const [mode, setMode] = useState<"create" | "append">(tab.props?.table ? "append" : "create");
  const [table, setTable] = useState<string>(tab.props?.table ?? "");
  const [newName, setNewName] = useState("");
  const [tables, setTables] = useState<string[]>([]);
  const [tableCols, setTableCols] = useState<Array<{ name: string; data_type: string }>>([]);
  const [mapping, setMapping] = useState<MapRow[]>([]);
  const [truncate, setTruncate] = useState(false);
  const [onError, setOnError] = useState<"abort" | "skip">("abort");
  const [error, setError] = useState<string | null>(null);
  const job = useJob((r) => {
    if (r.status === "finished") {
      app.invalidate(keys.folder(sid, dbid, "tables"));
      app.toast(t("Imported {n} row(s)", { n: String(r.inserted) }), "ok");
    }
  });

  useEffect(() => {
    api.objects(sid, dbid, "tables").then((t) => setTables(t.map((x) => x.name))).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (mode !== "append" || !table) { setTableCols([]); return; }
    api.object(sid, dbid, "tables", table).then((o) => setTableCols((o.columns ?? []).filter((c) => !c.computed)
      .map((c) => ({ name: c.name, data_type: c.data_type })))).catch((e) => setError(errMsg(e)));
  }, [mode, table]);

  // initial mapping: by (normalized) name for existing tables, suggested names for new ones
  useEffect(() => {
    if (!preview) return;
    setMapping(preview.columns.map((c) => {
      const match = tableCols.find((t) => norm(t.name) === norm(c.name));
      return {
        source: c.name,
        include: mode === "create" || !!match,
        target: mode === "create" ? c.suggested_name : match?.name ?? "",
        type: c.type,
      };
    }));
  }, [preview, tableCols, mode]);

  const loadPreview = async (uploadId: string, fmt: string, o = opts) => {
    setError(null);
    try {
      const p = await schemaApi.preview(uploadId, fmt || null, {
        header: o.header, encoding: o.encoding, ...(o.delimiter ? { delimiter: o.delimiter } : {}), ...(o.sheet ? { sheet: o.sheet } : {}),
      });
      setPreview(p);
      if (!fmt) setFormat(p.format);
    } catch (e) {
      setError(errMsg(e));
    }
  };
  const choose = async (f: File | undefined) => {
    if (!f) return;
    setFile(f);
    setPreview(null);
    setError(null);
    setProgress(0);
    try {
      const up = await uploadFile(f, setProgress);
      setNewName(f.name.replace(/\.[^.]+$/, "").toUpperCase().replace(/[^A-Z0-9_]/g, "_").replace(/^(\d)/, "T_$1").slice(0, 31));
      await loadPreview(up.upload_id, "");
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setProgress(null);
    }
  };
  const setOpt = (k: string, v: unknown) => {
    const o = { ...opts, [k]: v };
    setOpts(o);
    if (preview && k !== "empty_as_null") loadPreview(preview.upload_id, format, o);
  };

  const start = async () => {
    if (!preview) return;
    setError(null);
    const target = mode === "create" ? newName.trim() : table;
    try {
      const j = await schemaApi.importData(sid, dbid, {
        upload_id: preview.upload_id, format, table: target, create: mode === "create", truncate, on_error: onError,
        options: { header: opts.header, encoding: opts.encoding, empty_as_null: opts.empty_as_null,
          ...(opts.delimiter ? { delimiter: opts.delimiter } : {}), ...(opts.sheet ? { sheet: opts.sheet } : {}) },
        columns: mapping.filter((m) => m.include && m.target).map((m) => ({ source: m.source, target: m.target, type: m.type })),
        delete_upload: true,
      });
      job.start(j);
    } catch (e) {
      setError(errMsg(e));
    }
  };
  const target = mode === "create" ? newName.trim() : table;
  const ready = !!preview && !!target && mapping.some((m) => m.include && m.target) && !job.running;

  return (
    <div className="sch-col sch-scroll">
      <div className="sch-steps">
        <section>
          <h3>{t("1. File")}</h3>
          <div className="sch-row">
            <label className="btn primary"><IconUpload /> {file ? t("Change file…") : t("Choose CSV, JSON or XLSX…")}
              <input type="file" accept=".csv,.tsv,.txt,.json,.ndjson,.jsonl,.xlsx" style={{ display: "none" }} onChange={(e) => choose(e.target.files?.[0])} />
            </label>
            {file && <span>{file.name} <span className="muted">({(file.size / 1024).toFixed(1)} KB)</span></span>}
            {progress !== null && <span className="muted"><IconSpinner /> {t("uploading {pct}%", { pct: Math.round(progress * 100) })}</span>}
          </div>
          {preview && (
            <div className="sch-row">
              <label>{t("Format")} <select className="text" value={format} onChange={(e) => { setFormat(e.target.value); loadPreview(preview.upload_id, e.target.value); }}>
                <option value="csv">CSV</option><option value="json">JSON</option><option value="xlsx">XLSX</option></select></label>
              {format === "csv" && <>
                <label>{t("Delimiter")} <select className="text" value={opts.delimiter} onChange={(e) => setOpt("delimiter", e.target.value)}>
                  <option value="">{t("auto ({delimiter})", { delimiter: preview.delimiter === "\t" ? t("tab") : preview.delimiter ?? "" })}</option>
                  <option value=",">,</option><option value=";">;</option><option value={"\t"}>{t("tab")}</option><option value="|">|</option></select></label>
                <label>{t("Encoding")} <select className="text" value={opts.encoding} onChange={(e) => setOpt("encoding", e.target.value)}>
                  <option value="utf-8-sig">UTF-8</option><option value="latin-1">Latin-1</option><option value="cp1252">Windows-1252</option></select></label>
              </>}
              {format === "xlsx" && preview.sheets.length > 1 && (
                <label>{t("Sheet")} <select className="text" value={opts.sheet} onChange={(e) => setOpt("sheet", e.target.value)}>
                  {preview.sheets.map((s) => <option key={s} value={s}>{s}</option>)}</select></label>
              )}
              {format !== "json" && <label><input type="checkbox" checked={opts.header} onChange={(e) => setOpt("header", e.target.checked)} /> {t("First row is a header")}</label>}
              <label><input type="checkbox" checked={opts.empty_as_null} onChange={(e) => setOpt("empty_as_null", e.target.checked)} /> {t("Empty text as NULL")}</label>
            </div>
          )}
          {preview && (
            <div className="grid-wrap" style={{ maxHeight: 230 }}>
              <table className="grid">
                <thead><tr><th className="rn">#</th>{preview.columns.map((c) => <th key={c.name}>{c.name}<span className="ty">{c.type}</span></th>)}</tr></thead>
                <tbody>{preview.rows.map((r, i) => (
                  <tr key={i}><td className="rn">{i + 1}</td>{r.map((v, j) => <td key={j} className={v === null ? "null" : ""}>{v === null ? "NULL" : cellText(v)}</td>)}</tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </section>
        {preview && (
          <section>
            <h3>{t("2. Target table")}</h3>
            <div className="sch-row">
              <label><input type="radio" checked={mode === "create"} onChange={() => setMode("create")} /> {t("New table")}</label>
              <input className="text" disabled={mode !== "create"} value={newName} onChange={(e) => setNewName(e.target.value.toUpperCase())} placeholder="TABLE_NAME" />
              <label><input type="radio" checked={mode === "append"} onChange={() => setMode("append")} /> {t("Existing table")}</label>
              <select className="text" disabled={mode !== "append"} value={table} onChange={(e) => setTable(e.target.value)}>
                <option value="">{t("— choose —")}</option>
                {tables.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              {mode === "append" && <label><input type="checkbox" checked={truncate} onChange={(e) => setTruncate(e.target.checked)} /> {t("Delete existing rows first")}</label>}
            </div>
          </section>
        )}
        {preview && (
          <section>
            <h3>{t("3. Column mapping and types")}</h3>
            <table className="sch-map">
              <thead><tr><th /><th>{t("File column")}</th><th>{t("Sample")}</th><th>{t("Table column")}</th><th>{mode === "create" ? t("Type") : t("Column type")}</th></tr></thead>
              <tbody>
                {mapping.map((m, i) => (
                  <tr key={m.source} className={m.include ? "" : "off"}>
                    <td><input type="checkbox" checked={m.include} onChange={(e) => setMapping(mapping.map((x, j) => (j === i ? { ...x, include: e.target.checked } : x)))} /></td>
                    <td><b>{m.source}</b></td>
                    <td className="muted">{cellText(preview.rows[0]?.[i] ?? "")}</td>
                    <td>{mode === "create" ? (
                      <input className="text" value={m.target} onChange={(e) => setMapping(mapping.map((x, j) => (j === i ? { ...x, target: e.target.value.toUpperCase() } : x)))} />
                    ) : (
                      <select className="text" value={m.target} onChange={(e) => setMapping(mapping.map((x, j) => (j === i ? { ...x, target: e.target.value, include: !!e.target.value } : x)))}>
                        <option value="">{t("— skip —")}</option>
                        {tableCols.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                      </select>
                    )}</td>
                    <td>{mode === "create" ? (
                      <input className="text" list="imp-types" value={m.type} onChange={(e) => setMapping(mapping.map((x, j) => (j === i ? { ...x, type: e.target.value.toUpperCase() } : x)))} />
                    ) : <span className="muted">{tableCols.find((c) => c.name === m.target)?.data_type ?? ""}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <datalist id="imp-types">{["INTEGER", "BIGINT", "NUMERIC(18,2)", "DOUBLE PRECISION", "VARCHAR(50)", "VARCHAR(255)", "DATE", "TIMESTAMP", "TIME", "BOOLEAN", "BLOB SUB_TYPE TEXT"].map((t) => <option key={t} value={t} />)}</datalist>
            <p className="muted">{t("Values are converted to the column types (numbers, dates in ISO or local formats, booleans as true/false/1/0/yes/no). Types of a new table are inferred from the first {n} rows.", { n: preview.sampled_rows.toLocaleString() })}</p>
          </section>
        )}
        {preview && (
          <section>
            <h3>{t("4. Import")}</h3>
            <div className="sch-row">
              <label>{t("On error")} <select className="text" value={onError} onChange={(e) => setOnError(e.target.value as "abort" | "skip")}>
                <option value="abort">{t("roll back everything")}</option><option value="skip">{t("skip bad rows and report them")}</option></select></label>
              <button className="btn primary" disabled={!ready} onClick={start}>{job.running ? <IconSpinner /> : <IconPlay />} {t("Import into {table}", { table: target || "…" })}</button>
              {job.result?.status === "finished" && (
                <button className="btn" onClick={() => app.openTab({ kind: "data", title: target, serverId: sid, dbId: dbid, otype: "tables", name: target })}><IconTable /> {t("View data")}</button>
              )}
            </div>
            <p className="muted">{t("All rows are inserted in one transaction; the file is read as a stream on the server.")}</p>
            <JobStatus result={job.result} log={job.log} />
          </section>
        )}
        {error && <div className="err">{error}</div>}
      </div>
    </div>
  );
}

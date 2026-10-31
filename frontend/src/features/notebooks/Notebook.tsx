/** The notebook editor: SQL cells with their results and charts, Markdown notes, autosave, sharing, history. */
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { SqlEditor } from "../../components/SqlEditor";
import { ResultGrid } from "../../components/ResultGrid";
import { Markdown } from "../../components/Markdown";
import { Modal } from "../../components/Modal";
import { fmtTime } from "../maintenance/api";
import { ChartView } from "../charts/ChartView";
import { defaultConfig, type ChartConfig } from "../charts/shape";
import "../charts/charts.css";
import { nbApi, newId, type Cell, type Notebook, type Output, type ResultOut, type RevisionInfo } from "./api";

const SAVE_DELAY = 1200;
const RUN_ROWS = 1000;

type SaveState = "saved" | "dirty" | "saving" | "error" | "conflict" | "readonly";

function toOutput(res: Awaited<ReturnType<typeof api.runQuery>>, user: string | null): Output {
  return {
    ran_at: Date.now() / 1000, ran_by: user, elapsed_ms: res.elapsed_ms, error: res.error?.message ?? null,
    results: res.results.filter((r) => r.kind !== "ignored").slice(-5).map((r): ResultOut => ({
      kind: r.kind, message: r.message ?? "", columns: r.columns ?? [], rows: (r.rows ?? []).slice(0, RUN_ROWS),
      row_count: r.row_count ?? null, truncated: !!r.truncated, elapsed_ms: r.elapsed_ms ?? null,
    })),
  };
}

function OutputView(props: { cell: Cell; onChart: (c: ChartConfig | null) => void; readOnly: boolean }) {
  const o = props.cell.output;
  if (!o) return null;
  const selects = o.results.filter((r) => r.kind === "select");
  const last = selects[selects.length - 1];
  const chart = props.cell.chart;
  const usable = !!last && last.columns.length > 0 && (!chart || chart.x < last.columns.length);
  return (
    <div className="nb-out">
      {o.results.filter((r) => r.kind === "command" && r.message).map((r, i) => <div key={i} className="muted">{r.message}</div>)}
      {o.error && <div className="err" role="alert">{o.error}</div>}
      {last && last.rows.length > 0 && (
        <div className="mt-row">
          <button className="btn small" aria-pressed={!!chart} disabled={props.readOnly && !chart}
            onClick={() => props.onChart(chart ? null : defaultConfig(last.columns, last.rows))}>
            {chart ? t("Hide the chart") : t("Chart the result")}
          </button>
        </div>
      )}
      {chart && usable && (
        <div className="nb-chartbox">
          <ChartView key={last.columns.map((c) => c.name).join("|")} config={chart} onConfig={props.onChart}
            ctx={{ columns: last.columns, rows: last.rows, hasMore: false, loadingMore: false, loadMore: async () => ({ fetched: 0, hasMore: false }) }} />
        </div>
      )}
      {selects.map((r, i) => (
        <div key={i} className="nb-grid" role="region" aria-label={t("Result {n}", { n: i + 1 })}>
          <ResultGrid columns={r.columns} rows={r.rows} name="notebook" />
          <div className="muted nb-meta">{r.message}{r.truncated ? ` · ${t("only the first {n} rows are kept", { n: r.rows.length })}` : ""}</div>
        </div>
      ))}
      <div className="muted nb-meta">{t("Run {time}", { time: fmtTime(o.ran_at) })}{o.ran_by ? ` · ${o.ran_by}` : ""}{o.elapsed_ms != null ? ` · ${Math.round(o.elapsed_ms)} ms` : ""}</div>
    </div>
  );
}

function CellView(props: {
  cell: Cell; index: number; count: number; readOnly: boolean; running: boolean; busy: boolean;
  onChange: (c: Cell) => void; onRun: (sql?: string) => void; onMove: (d: -1 | 1) => void; onDelete: () => void;
  onAdd: (type: Cell["type"]) => void; onOpenSql: (sql: string) => void;
}) {
  const { cell, readOnly } = props;
  const [editing, setEditing] = useState(cell.type === "markdown" && !cell.source);
  const kind = cell.type === "sql" ? t("SQL") : t("Note");
  return (
    <section className={"nb-cell " + cell.type} aria-label={t("Cell {n}: {kind}", { n: props.index + 1, kind })}>
      <div className="nb-cellbar">
        <span className="nb-kind">{props.index + 1} · {kind}</span>
        {cell.type === "sql" && <button className="btn small primary" disabled={props.busy} onClick={() => props.onRun()}>
          {props.running ? t("Running…") : t("Run")}</button>}
        {cell.type === "markdown" && !readOnly && <button className="btn small" aria-pressed={editing} onClick={() => setEditing(!editing)}>{editing ? t("Done") : t("Edit")}</button>}
        <span style={{ flex: 1 }} />
        {!readOnly && <>
          <button className="btn small" disabled={props.index === 0} onClick={() => props.onMove(-1)} aria-label={t("Move up")} title={t("Move up")}>↑</button>
          <button className="btn small" disabled={props.index === props.count - 1} onClick={() => props.onMove(1)} aria-label={t("Move down")} title={t("Move down")}>↓</button>
          <button className="btn small" onClick={() => props.onAdd("sql")}>{t("+ SQL")}</button>
          <button className="btn small" onClick={() => props.onAdd("markdown")}>{t("+ Note")}</button>
          <button className="btn small" onClick={props.onDelete} aria-label={t("Delete cell")} title={t("Delete cell")}>✕</button>
        </>}
      </div>
      {cell.type === "sql" ? (
        <div className="nb-editor">
          <SqlEditor value={cell.source} readOnly={false} placeholder={t("SELECT … (Ctrl+Enter runs the cell)")}
            onChange={(v) => props.onChange({ ...cell, source: v })} onExecute={(sql) => props.onRun(sql)} />
        </div>
      ) : editing ? (
        <textarea className="text nb-md" aria-label={t("Note (Markdown)")} value={cell.source} rows={Math.max(3, cell.source.split("\n").length)}
          placeholder={t("Notes in Markdown: ## headings, - lists, **bold**, `code`")} onChange={(e) => props.onChange({ ...cell, source: e.target.value })} />
      ) : (
        <div className="nb-note" onDoubleClick={() => !readOnly && setEditing(true)}>
          {cell.source.trim() ? <Markdown text={cell.source} onOpenSql={props.onOpenSql} /> : <span className="muted">{t("Empty note")}</span>}
        </div>
      )}
      {cell.type === "sql" && <OutputView cell={cell} readOnly={readOnly} onChart={(chart) => props.onChange({ ...cell, chart })} />}
    </section>
  );
}

function History(props: { nb: Notebook; onRestored: (nb: Notebook) => void; onClose: () => void }) {
  const app = useApp();
  const [revs, setRevs] = useState<RevisionInfo[] | null>(null);
  useEffect(() => { nbApi.revisions(props.nb.id).then((r) => setRevs(r.revisions)).catch((e) => app.toast(errMsg(e), "error")); }, []);
  return (
    <Modal title={t("History of {name}", { name: props.nb.name })} onClose={props.onClose}>
      {!revs ? <div className="muted">{t("Loading…")}</div> : (
        <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Revisions")}><table className="grid">
          <thead><tr><th>{t("Revision")}</th><th>{t("Saved")}</th><th>{t("By")}</th><th>{t("Cells")}</th><th /></tr></thead>
          <tbody>{revs.map((r, i) => (
            <tr key={r.rev}><td className="num">{r.rev}</td><td>{fmtTime(r.at)}</td><td>{r.author ?? "—"}</td><td className="num">{r.cells}</td>
              <td>{i > 0 && props.nb.can_edit && <button className="btn small" onClick={async () => {
                if (!confirm(t("Restore revision {rev}? The current content stays in the history.", { rev: r.rev }))) return;
                try { props.onRestored(await nbApi.restore(props.nb.id, r.rev)); props.onClose(); } catch (e) { app.toast(errMsg(e), "error"); }
              }}>{t("Restore")}</button>}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </Modal>
  );
}

export function NotebookTab(props: { id: string; visible: boolean; user: string | null; serverMode: boolean }) {
  const app = useApp();
  const [nb, setNb] = useState<Notebook | null>(null);
  const [cells, setCells] = useState<Cell[]>([]);
  const [meta, setMeta] = useState<{ name: string; description: string; server_id: string | null; database_id: string | null; visibility: Notebook["visibility"] }>({ name: "", description: "", server_id: null, database_id: null, visibility: "private" });
  const [state, setState] = useState<SaveState>("saved");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [history, setHistory] = useState(false);
  const session = useRef<{ key: string; id: string } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const pending = useRef<null | (() => Promise<void>)>(null);
  const latest = useRef({ cells, meta, nb });
  latest.current = { cells, meta, nb };

  const adopt = (n: Notebook) => {
    setNb(n); setCells(n.cells);
    setMeta({ name: n.name, description: n.description, server_id: n.server_id, database_id: n.database_id, visibility: n.visibility });
    setState(n.can_edit ? "saved" : "readonly");
  };
  useEffect(() => { nbApi.get(props.id).then(adopt).catch((e) => setError(errMsg(e))); }, [props.id]);
  useEffect(() => () => {
    window.clearTimeout(timer.current);
    pending.current?.();
    if (session.current) api.closeSession(session.current.id).catch(() => undefined);
  }, []);

  const save = async () => {
    const { nb: cur, cells: cs, meta: m } = latest.current;
    if (!cur || !cur.can_edit) return;
    pending.current = null;
    setState("saving");
    try {
      const n = await nbApi.save(cur.id, { base_rev: cur.rev, ...m, cells: cs });
      setNb(n);
      setState((s) => (s === "saving" ? "saved" : s));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { setState("conflict"); setError(e.message); }
      else { setState("error"); setError(errMsg(e)); }
    }
  };
  const touch = () => {
    if (!latest.current.nb?.can_edit) return;
    setState("dirty");
    pending.current = save;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { save(); }, SAVE_DELAY);
  };
  // every change of content schedules a save
  const changeCells = (f: (c: Cell[]) => Cell[]) => { setCells((c) => { const n = f(c); latest.current.cells = n; return n; }); touch(); };
  const changeMeta = (p: Partial<typeof meta>) => { setMeta((m) => { const n = { ...m, ...p }; latest.current.meta = n; return n; }); touch(); };

  const ensureSession = async (): Promise<string> => {
    const { server_id, database_id } = latest.current.meta;
    if (!server_id || !database_id) throw new Error(t("Pick the database this notebook runs on"));
    const key = `${server_id}/${database_id}`;
    if (session.current?.key === key) return session.current.id;
    if (session.current) await api.closeSession(session.current.id).catch(() => undefined);
    const s = await api.openSession(server_id, database_id);
    session.current = { key, id: s.session_id };
    return s.session_id;
  };

  const run = async (id: string, sql?: string): Promise<boolean> => {
    const cell = latest.current.cells.find((c) => c.id === id);
    if (!cell || cell.type !== "sql" || !(sql ?? cell.source).trim()) return true;
    setRunning(id);
    let output: Output;
    try {
      let sess = await ensureSession();
      const body = { sql: sql ?? cell.source, autocommit: true, max_rows: RUN_ROWS };
      let res;
      try { res = await api.runQuery(sess, body); } catch (e) {
        if (!(e instanceof ApiError && e.status === 404)) throw e;
        session.current = null; // the session expired: open a new one once
        sess = await ensureSession();
        res = await api.runQuery(sess, body);
      }
      output = toOutput(res, props.user);
    } catch (e) {
      output = { ran_at: Date.now() / 1000, ran_by: props.user, elapsed_ms: null, error: errMsg(e), results: [] };
    }
    setRunning(null);
    if (latest.current.nb?.can_edit) changeCells((cs) => cs.map((c) => (c.id === id ? { ...c, output } : c)));
    else setCells((cs) => cs.map((c) => (c.id === id ? { ...c, output } : c)));
    return !output.error;
  };
  const runAll = async () => {
    for (const c of latest.current.cells) {
      if (c.type === "sql" && !(await run(c.id))) break;
    }
  };

  if (error && !nb) return <div className="pad err">{error}</div>;
  if (!nb) return <div className="pad muted">{t("Loading…")}</div>;
  const ro = !nb.can_edit;
  const dbs = app.servers.flatMap((s) => s.databases.map((d) => ({ value: `${s.id}/${d.id}`, label: `${s.name} · ${d.name}` })));
  const curDb = meta.server_id && meta.database_id ? `${meta.server_id}/${meta.database_id}` : "";
  const add = (at: number, type: Cell["type"]) => changeCells((cs) => [...cs.slice(0, at), { id: newId(), type, source: "", chart: null, output: null }, ...cs.slice(at)]);
  const stateText: Record<SaveState, string> = {
    saved: t("Saved"), dirty: t("Unsaved changes"), saving: t("Saving…"), error: t("Not saved"),
    conflict: t("Changed by someone else"), readonly: t("Read only"),
  };

  return (
    <div className="nb">
      <div className="nb-head">
        <input className="text nb-title" aria-label={t("Notebook name")} value={meta.name} readOnly={ro}
          onChange={(e) => changeMeta({ name: e.target.value })} onBlur={() => !meta.name.trim() && changeMeta({ name: nb.name })} />
        <select className="text" aria-label={t("Database")} value={curDb} disabled={ro && !!curDb}
          onChange={(e) => { const [s, d] = e.target.value.split("/"); if (ro) setMeta({ ...meta, server_id: s || null, database_id: d || null }); else changeMeta({ server_id: s || null, database_id: d || null }); }}>
          <option value="">{t("Pick a database…")}</option>
          {curDb && !dbs.some((d) => d.value === curDb) && <option value={curDb}>{t("(not available to you)")}</option>}
          {dbs.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
        </select>
        {props.serverMode && (
          <select className="text" aria-label={t("Visibility")} value={meta.visibility} disabled={ro || !nb.mine} onChange={(e) => changeMeta({ visibility: e.target.value as Notebook["visibility"] })}>
            <option value="private">{t("Private")}</option>
            <option value="shared">{t("Shared with every fbadmin user")}</option>
          </select>
        )}
        <span className={"nb-state " + state} role="status">{stateText[state]}</span>
        <span style={{ flex: 1 }} />
        <button className="btn small primary" disabled={!!running} onClick={runAll}>{t("Run all")}</button>
        {!ro && <button className="btn small" onClick={() => { if (confirm(t("Clear the results of every cell?"))) changeCells((cs) => cs.map((c) => ({ ...c, output: null }))); }}>{t("Clear results")}</button>}
        <button className="btn small" onClick={() => setHistory(true)}>{t("History…")}</button>
        <a className="btn small" href={nbApi.exportUrl(nb.id, "md")} download>{t("Export Markdown")}</a>
        <a className="btn small" href={nbApi.exportUrl(nb.id, "json")} download>{t("Export notebook")}</a>
        <button className="btn small" onClick={async () => {
          try { const cp = await nbApi.copy(nb.id); openNotebook(app, cp.id, cp.name); } catch (e) { app.toast(errMsg(e), "error"); }
        }}>{t("Save a copy")}</button>
        {nb.can_edit && <button className="btn small danger" onClick={async () => {
          if (!confirm(t("Delete the notebook {name}?", { name: nb.name }))) return;
          try { window.clearTimeout(timer.current); pending.current = null; await nbApi.remove(nb.id); app.toast(t("Notebook deleted"), "ok"); } catch (e) { app.toast(errMsg(e), "error"); }
        }}>{t("Delete")}</button>}
      </div>
      <textarea className="text nb-desc" aria-label={t("Description")} placeholder={ro ? "" : t("What this notebook is for (optional)")} rows={1}
        value={meta.description} readOnly={ro} onChange={(e) => changeMeta({ description: e.target.value })} />
      {ro && <div className="nb-banner">{t("Shared by {owner}. You can run the cells, but changes and results are not saved: save a copy to keep them.", { owner: nb.owner ?? "?" })}</div>}
      {nb.results_hidden && <div className="nb-banner">{t("The saved results are hidden: your fbadmin permissions do not allow reading this notebook's database.")}</div>}
      {state === "conflict" && <div className="nb-banner err">
        {error}{" "}
        <button className="btn small" onClick={async () => { adopt(await nbApi.get(nb.id)); setError(null); }}>{t("Load their version")}</button>{" "}
        <button className="btn small" onClick={async () => {
          try {
            const cp = await nbApi.create({ name: `${meta.name} (${t("my version")})`, server_id: meta.server_id, database_id: meta.database_id, cells });
            openNotebook(app, cp.id, cp.name);
          } catch (e) { app.toast(errMsg(e), "error"); }
        }}>{t("Save mine as a new notebook")}</button>
      </div>}
      {state === "error" && error && <div className="nb-banner err">{error} <button className="btn small" onClick={save}>{t("Retry")}</button></div>}
      <div className="nb-cells">
        {cells.map((c, i) => (
          <CellView key={c.id} cell={c} index={i} count={cells.length} readOnly={ro} running={running === c.id} busy={!!running}
            onChange={(n) => (ro ? setCells((cs) => cs.map((x) => (x.id === n.id ? n : x))) : changeCells((cs) => cs.map((x) => (x.id === n.id ? n : x))))}
            onRun={(sql) => run(c.id, sql)}
            onMove={(d) => changeCells((cs) => { const n = [...cs]; [n[i], n[i + d]] = [n[i + d], n[i]]; return n; })}
            onDelete={() => { if (c.source.trim() && !confirm(t("Delete this cell?"))) return; changeCells((cs) => cs.filter((x) => x.id !== c.id)); }}
            onAdd={(type) => add(i + 1, type)}
            onOpenSql={(sql) => meta.server_id && meta.database_id && app.openQueryTool(meta.server_id, meta.database_id, sql)} />
        ))}
        {!ro && <div className="nb-add">
          <button className="btn small" onClick={() => add(cells.length, "sql")}>{t("+ SQL cell")}</button>
          <button className="btn small" onClick={() => add(cells.length, "markdown")}>{t("+ Note")}</button>
        </div>}
      </div>
      {history && <History nb={nb} onClose={() => setHistory(false)} onRestored={adopt} />}
    </div>
  );
}

export function openNotebook(app: ReturnType<typeof useApp>, id: string, name: string) {
  app.openTab({ kind: "notebook", title: name, props: { id } });
}

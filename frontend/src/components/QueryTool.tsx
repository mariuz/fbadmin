import { useCallback, useEffect, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import { api } from "../api";
import { errMsg, keys, useApp } from "../context";
import type { ScriptResult, SessionOptions, StatementResult } from "../types";
import { Modal } from "./Modal";
import { PlanDiff, PlanGraph } from "./PlanGraph";
import { getPrefs } from "../prefs";
import { AI_TASKS, AssistantPane, askAi, useAiStatus } from "./Assistant";
import { ResultGrid, download } from "./ResultGrid";
import {
  SqlEditor, clearErrors, formatEditor, lineColToOffset, selectionOrAll, showError, type ExtraCompletions,
} from "./SqlEditor";
import { IconDownload, IconPlan, IconPlay, IconPlus, IconSpinner, IconTrash, IconUpload, IconX } from "./Icons";

// ----------------------------------------------------------- persistence
interface HistoryEntry { sql: string; at: number; ok: boolean; ms: number }
interface Snippet { name: string; sql: string }
const HISTORY_KEY = "fbadmin.history";
const SNIPPETS_KEY = "fbadmin.snippets";
const PARAMS_KEY = "fbadmin.params";

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}
function save(key: string, v: unknown) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* storage unavailable */ }
}
function pushHistory(e: HistoryEntry) {
  save(HISTORY_KEY, [e, ...load<HistoryEntry[]>(HISTORY_KEY, []).filter((x) => x.sql !== e.sql)].slice(0, 200));
}

const DEFAULT_SNIPPETS: Snippet[] = [
  { name: "Top statements by run time (MON$)", sql: "SELECT s.MON$ATTACHMENT_ID, s.MON$TIMESTAMP, s.MON$SQL_TEXT\nFROM MON$STATEMENTS s\nWHERE s.MON$STATE = 1\nORDER BY s.MON$TIMESTAMP;\n" },
  { name: "Table sizes (record count estimate)", sql: "SELECT TRIM(r.RDB$RELATION_NAME) AS TABLE_NAME,\n  (SELECT COUNT(*) FROM RDB$RELATION_FIELDS f WHERE f.RDB$RELATION_NAME = r.RDB$RELATION_NAME) AS COLUMNS\nFROM RDB$RELATIONS r\nWHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND r.RDB$VIEW_BLR IS NULL\nORDER BY 1;\n" },
  { name: "Execute block", sql: "EXECUTE BLOCK RETURNS (N INTEGER)\nAS\nBEGIN\n  N = 1;\n  SUSPEND;\nEND;\n" },
  { name: "Context variables", sql: "SELECT\n  RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') AS ENGINE,\n  RDB$GET_CONTEXT('SYSTEM', 'CLIENT_ADDRESS') AS CLIENT,\n  CURRENT_USER, CURRENT_ROLE, CURRENT_CONNECTION, CURRENT_TRANSACTION\nFROM RDB$DATABASE;\n" },
];

const DDL_RE = /^\s*(create|alter|drop|recreate|declare|comment|grant|revoke)\b/im;
const ISOLATIONS: Array<[string, string, number]> = [
  ["read_committed", "Read committed", 3],
  ["read_consistency", "Read committed – read consistency", 4],
  ["read_committed_no_rec_version", "Read committed – no record version", 3],
  ["snapshot", "Snapshot (concurrency)", 3],
  ["snapshot_table_stability", "Snapshot table stability (consistency)", 3],
];
const STAT_LABELS: Array<[string, string]> = [
  ["fetches", "Page fetches"], ["reads", "Page reads"], ["writes", "Page writes"], ["marks", "Page marks"],
  ["seq_reads", "Seq. reads"], ["idx_reads", "Index reads"], ["inserts", "Inserts"], ["updates", "Updates"],
  ["deletes", "Deletes"], ["backouts", "Backouts"], ["purges", "Purges"], ["expunges", "Expunges"],
];

// ------------------------------------------------------------- dialogs
function ParamsDialog(props: { names: string[]; onDone: (v: Record<string, unknown> | null) => void }) {
  const remembered = load<Record<string, string | null>>(PARAMS_KEY, {});
  const [vals, setVals] = useState<Record<string, string | null>>(
    Object.fromEntries(props.names.map((n) => [n, n in remembered ? remembered[n] : ""])),
  );
  const submit = () => {
    save(PARAMS_KEY, { ...load(PARAMS_KEY, {}), ...vals });
    props.onDone(vals);
  };
  return (
    <Modal
      title="Query parameters"
      onClose={() => props.onDone(null)}
      footer={<>
        <button className="btn" onClick={() => props.onDone(null)}>Cancel</button>
        <button className="btn primary" onClick={submit}>Execute</button>
      </>}
    >
      <div className="form" onKeyDown={(e) => e.key === "Enter" && submit()}>
        {props.names.map((n, i) => (
          <div key={n} style={{ display: "contents" }}>
            <label className="l mono">{n.startsWith("$") ? `? #${n.slice(1)}` : `:${n}`}</label>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input className="text mono" style={{ flex: 1 }} autoFocus={i === 0} disabled={vals[n] === null}
                value={vals[n] ?? ""} placeholder={vals[n] === null ? "NULL" : ""}
                onChange={(e) => setVals({ ...vals, [n]: e.target.value })} />
              <label className="check"><input type="checkbox" checked={vals[n] === null}
                onChange={(e) => setVals({ ...vals, [n]: e.target.checked ? null : "" })} /> NULL</label>
            </div>
          </div>
        ))}
        <span />
        <span className="muted">Values are sent as text; Firebird converts them to the column type.</span>
      </div>
    </Modal>
  );
}

function TxSettings(props: { options: SessionOptions; onApply: (o: Partial<SessionOptions>) => Promise<void>; onClose: () => void }) {
  const [o, setO] = useState(props.options);
  const [err, setErr] = useState<string | null>(null);
  const v4 = o.engine_major >= 4;
  return (
    <div className="popover" onMouseDown={(e) => e.stopPropagation()}>
      <div className="form">
        <label className="l">Isolation</label>
        <select className="text" value={o.isolation} onChange={(e) => setO({ ...o, isolation: e.target.value })}>
          {ISOLATIONS.map(([k, label, min]) => <option key={k} value={k} disabled={o.engine_major < min}>{label}</option>)}
        </select>
        <span />
        <label className="check"><input type="checkbox" checked={o.read_only} onChange={(e) => setO({ ...o, read_only: e.target.checked })} /> Read only</label>
        <label className="l">Lock resolution</label>
        <select className="text" value={o.wait ? "wait" : "nowait"} onChange={(e) => setO({ ...o, wait: e.target.value === "wait" })}>
          <option value="wait">Wait</option>
          <option value="nowait">No wait</option>
        </select>
        <label className="l">Lock timeout (s)</label>
        <input className="text" type="number" min={0} disabled={!o.wait} value={o.lock_timeout ?? ""} placeholder="infinite"
          onChange={(e) => setO({ ...o, lock_timeout: e.target.value ? Number(e.target.value) : null })} />
        <label className="l">Statement timeout</label>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input className="text" type="number" min={0} disabled={!v4} value={o.statement_timeout || ""} placeholder={v4 ? "none" : "Firebird 4+"}
            onChange={(e) => setO({ ...o, statement_timeout: Number(e.target.value || 0) })} style={{ flex: 1 }} />
          <span className="muted">ms</span>
        </div>
      </div>
      {err && <div className="err" style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{err}</div>}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
        <button className="btn" onClick={props.onClose}>Close</button>
        <button className="btn primary" onClick={async () => {
          try {
            await props.onApply({ isolation: o.isolation, read_only: o.read_only, wait: o.wait, lock_timeout: o.wait ? o.lock_timeout : null, statement_timeout: v4 ? o.statement_timeout : undefined });
            props.onClose();
          } catch (e) { setErr(errMsg(e)); }
        }}>Apply</button>
      </div>
    </div>
  );
}

/** Detailed plan with highlighted operators, tables and indices. */
function PlanTree(props: { text: string }) {
  return (
    <pre className="plan-tree">
      {props.text.split("\n").map((line, i) => {
        const parts = line.split(/(->\s*[A-Za-z][A-Za-z ]*?(?=\s"|$|\s\(|,)|Table (?:"[^"]+"\.)?"[^"]+"(?: as "[^"]+")?|Index (?:"[^"]+"\.)?"[^"]+")/g);
        return (
          <div key={i}>
            {parts.map((p, j) =>
              p.startsWith("->") ? <span key={j} className="op">{p}</span>
                : p.startsWith("Table ") ? <span key={j} className="tbl">{p}</span>
                : p.startsWith("Index ") ? <span key={j} className="idx">{p}</span>
                : <span key={j}>{p}</span>,
            )}
          </div>
        );
      })}
    </pre>
  );
}

function StatsTable(props: { stats: Record<string, number> }) {
  const items = STAT_LABELS.filter(([k]) => props.stats[k]);
  if (!items.length) return <span className="muted">no I/O</span>;
  return (
    <table className="stats-table"><tbody><tr>
      {items.map(([k, label]) => <td key={k}><span className="muted">{label}</span> {props.stats[k].toLocaleString()}</td>)}
    </tr></tbody></table>
  );
}

// ------------------------------------------------------------ query tool
export function QueryTool(props: { serverId: string; dbId: string; initialSql?: string; autorun?: boolean; visible: boolean }) {
  const app = useApp();
  const [sql, setSql] = useState(props.initialSql ?? "");
  const [session, setSession] = useState<string | null>(null);
  const [options, setOptions] = useState<SessionOptions | null>(null);
  const [result, setResult] = useState<ScriptResult | null>(null);
  const [running, setRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [autocommit, setAutocommit] = useState(true);
  const [txOpen, setTxOpen] = useState(false);
  const [maxRows, setMaxRows] = useState(1000);
  const [withPlan, setWithPlan] = useState(false);
  const [withStats, setWithStats] = useState(false);
  const [pane, setPane] = useState<string>("data");
  const [activeResult, setActiveResult] = useState(0);
  const [plan, setPlan] = useState<{ plan: string | null; detailed: string | null; sql?: string } | null>(null);
  const [pinned, setPinned] = useState<{ plan: string | null; detailed: string | null; sql?: string } | null>(null);
  const [planView, setPlanView] = useState<"graph" | "text" | "diff">("graph");
  const [history, setHistory] = useState<HistoryEntry[]>(() => load(HISTORY_KEY, []));
  const [snippets, setSnippets] = useState<Snippet[]>(() => load(SNIPPETS_KEY, DEFAULT_SNIPPETS));
  const [schema, setSchema] = useState<Record<string, string[]>>({});
  const [extra, setExtra] = useState<ExtraCompletions>({});
  const [editorH, setEditorH] = useState(260);
  const [showTx, setShowTx] = useState(false);
  const [paramReq, setParamReq] = useState<{ names: string[]; resolve: (v: Record<string, unknown> | null) => void } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const aiStatus = useAiStatus();
  const [ai, setAi] = useState<{ loading: boolean; task?: string; answer?: string; error?: string } | null>(null);
  const [aiMenu, setAiMenu] = useState(false);
  const view = useRef<EditorView | null>(null);
  const sessionRef = useRef<string | null>(null);

  const ensureSession = useCallback(async () => {
    if (sessionRef.current) return sessionRef.current;
    const r = await api.openSession(props.serverId, props.dbId);
    sessionRef.current = r.session_id;
    setSession(r.session_id);
    setOptions(r.options);
    return r.session_id;
  }, [props.serverId, props.dbId]);

  useEffect(() => {
    api.completion(props.serverId, props.dbId)
      .then((c) => {
        setSchema(c.relations);
        setExtra({ procedures: c.procedures, functions: c.functions, packages: c.packages });
      })
      .catch(() => {});
    ensureSession().catch(() => {});
    if (props.autorun && props.initialSql) run(props.initialSql);
    return () => {
      if (sessionRef.current) api.closeSession(sessionRef.current).catch(() => {});
    };
  }, []);

  useEffect(() => {
    if (!running) return;
    const t0 = performance.now();
    const t = setInterval(() => setElapsed(Math.round((performance.now() - t0) / 100) / 10), 100);
    return () => clearInterval(t);
  }, [running]);

  const askParams = (names: string[]) =>
    new Promise<Record<string, unknown> | null>((resolve) => setParamReq({ names, resolve }));

  const run = async (text?: string) => {
    const v = view.current;
    let q = text ?? (v ? selectionOrAll(v) : sql);
    let base = 0;
    if (v && text === undefined) {
      const sel = v.state.selection.main;
      base = sel.empty ? 0 : sel.from;
    } else if (v && text !== undefined) {
      const sel = v.state.selection.main;
      base = !sel.empty && v.state.sliceDoc(sel.from, sel.to) === text ? sel.from : 0;
    }
    if (!q.trim() || running) return;
    if (v) clearErrors(v);
    let params: Record<string, unknown> | undefined;
    try {
      const names = (await api.queryParams(q)).params;
      if (names.length) {
        const vals = await askParams(names);
        setParamReq(null);
        if (!vals) return;
        params = vals;
      }
    } catch { /* analysis is best-effort */ }
    setRunning(true);
    setPlan(null);
    const started = performance.now();
    try {
      let sid = await ensureSession();
      const body = { sql: q, autocommit, with_plan: withPlan, with_stats: withStats, max_rows: maxRows, params };
      let r: ScriptResult;
      try {
        r = await api.runQuery(sid, body);
      } catch (e: any) {
        if (e?.status === 404) {
          sessionRef.current = null; // session expired: reopen once
          sid = await ensureSession();
          r = await api.runQuery(sid, body);
        } else throw e;
      }
      setResult(r);
      setTxOpen(r.transaction_open);
      const selects = r.results.filter((x) => x.kind === "select");
      setActiveResult(Math.max(0, selects.length - 1));
      setPane(r.error ? "messages" : selects.length ? "data" : "messages");
      if (withPlan) {
        const p = r.results.map((x) => x.plan).filter(Boolean).join("\n\n");
        setPlan({ plan: p || null, detailed: null });
      }
      if (r.error && v) {
        const err = r.error;
        const failed = r.results[err.index];
        const pos = err.position ?? (failed ? { line: failed.line, column: (failed as any).col ?? 1 } : null);
        if (pos) {
          const off = base + lineColToOffset(q, pos.line, pos.column);
          if (off <= v.state.doc.length) showError(v, off, err.message);
        }
      }
      pushHistory({ sql: q, at: Date.now(), ok: !r.error, ms: Math.round(performance.now() - started) });
      setHistory(load(HISTORY_KEY, []));
      if (!r.error && DDL_RE.test(q) && autocommit) app.invalidate(keys.db(props.serverId, props.dbId));
    } catch (e) {
      setResult({ results: [], error: { message: errMsg(e), index: 0, line: 0, sql: q }, elapsed_ms: 0, transaction_open: txOpen });
      setPane("messages");
    } finally {
      setRunning(false);
      setCancelling(false);
    }
  };

  const cancel = async () => {
    if (!sessionRef.current) return;
    setCancelling(true);
    try {
      const r = await api.cancel(sessionRef.current);
      if (!r.cancelled) app.toast("Nothing to cancel (the statement may be finishing)", "info");
    } catch (e) {
      app.toast(errMsg(e), "error");
      setCancelling(false);
    }
  };

  const loadMore = async () => {
    if (!session || !result) return;
    setLoadingMore(true);
    try {
      const f = await api.fetchMore(session, maxRows);
      setResult((r) => {
        if (!r) return r;
        const results = r.results.map((x) => (x.kind === "select" && x === r.results.filter((y) => y.kind === "select").slice(-1)[0]
          ? { ...x, rows: [...(x.rows ?? []), ...f.rows], row_count: (x.row_count ?? 0) + f.rows.length, truncated: f.has_more }
          : x));
        return { ...r, results, cursor_open: f.has_more, transaction_open: f.transaction_open };
      });
      setTxOpen(f.transaction_open);
    } catch (e) {
      app.toast(errMsg(e), "error");
      setResult((r) => (r ? { ...r, cursor_open: false } : r));
    } finally {
      setLoadingMore(false);
    }
  };

  const explain = async () => {
    const q = view.current ? selectionOrAll(view.current) : sql;
    try {
      const sid = await ensureSession();
      const r = await api.explain(sid, q);
      setResult((x) => (x ? { ...x, cursor_open: false } : x)); // explain closes the open cursor
      setPlan(r.plan || r.detailed ? { ...r, sql: q } : { plan: "(no plan — the statement does not read tables)", detailed: null });
      setPane("plan");
    } catch (e) {
      setPlan({ plan: "Error: " + errMsg(e), detailed: null });
      setPane("plan");
    }
  };

  const txAction = async (what: "commit" | "rollback") => {
    if (!session) return;
    try {
      await (what === "commit" ? api.commit(session) : api.rollback(session));
      setTxOpen(false);
      setResult((r) => (r ? { ...r, cursor_open: false } : r));
      app.toast(what === "commit" ? "Transaction committed" : "Transaction rolled back");
      if (what === "commit") app.invalidate(keys.db(props.serverId, props.dbId));
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
  };

  const runAi = async (task: string) => {
    setAiMenu(false);
    const q = view.current ? selectionOrAll(view.current) : sql;
    const body: Record<string, unknown> = { task, sql: q, server_id: props.serverId, database_id: props.dbId };
    if (task === "convert_sql") {
      const d = window.prompt("Convert from which SQL dialect?", "MySQL");
      if (!d) return;
      body.dialect = d;
    }
    if (task === "ask") {
      const qn = window.prompt("Your question about this SQL / database:");
      if (!qn) return;
      body.question = qn;
    }
    if (task === "explain_error") {
      if (!result?.error) { app.toast("No error to explain — run a query first", "info"); return; }
      body.error = result.error.message;
      body.sql = result.error.sql;
    }
    setPane("assistant");
    setAi({ loading: true, task });
    try {
      setAi({ loading: false, task, answer: await askAi(body) });
    } catch (e) {
      setAi({ loading: false, task, error: errMsg(e) });
    }
  };

  const format = () => {
    if (!view.current) return;
    try {
      formatEditor(view.current);
    } catch (e) {
      app.toast("Cannot format: " + errMsg(e), "error");
    }
  };

  const insertText = (text: string) => {
    const v = view.current;
    if (!v) return setSql(sql + text);
    const sel = v.state.selection.main;
    const line = v.state.doc.lineAt(sel.from);
    const prefix = sel.from > line.from && line.text.trim() ? "\n" : "";
    v.dispatch({ changes: { from: sel.from, to: sel.to, insert: prefix + text }, selection: { anchor: sel.from + prefix.length + text.length } });
    v.focus();
  };

  const saveSnippet = () => {
    const text = view.current ? selectionOrAll(view.current) : sql;
    if (!text.trim()) return;
    const name = window.prompt("Snippet name", text.trim().split("\n")[0].slice(0, 60));
    if (!name) return;
    const next = [{ name, sql: text }, ...snippets.filter((s) => s.name !== name)];
    setSnippets(next);
    save(SNIPPETS_KEY, next);
    setPane("snippets");
  };

  const openFile = () => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = ".sql,.txt";
    inp.onchange = async () => {
      const f = inp.files?.[0];
      if (f) setSql(await f.text());
    };
    inp.click();
  };

  const startDrag = (e: React.MouseEvent) => {
    const y0 = e.clientY, h0 = editorH;
    const move = (ev: MouseEvent) => setEditorH(Math.max(80, h0 + ev.clientY - y0));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const selects = result?.results.filter((r) => r.kind === "select") ?? [];
  const current: StatementResult | undefined = selects[activeResult];
  const isLastSelect = current && current === selects[selects.length - 1];
  const txLabel = options ? ISOLATIONS.find(([k]) => k === options.isolation)?.[1].split(" – ")[0].replace(" (concurrency)", "") : "";

  return (
    <div className="qt" style={{ display: props.visible ? "flex" : "none" }}>
      <div className="toolbar">
        {running ? (
          <button className="btn stop" disabled={cancelling} onClick={cancel} title="Cancel the running statement">
            <IconSpinner className="spin" /> {cancelling ? "Cancelling…" : `Cancel (${elapsed.toFixed(1)} s)`}
          </button>
        ) : (
          <button className="btn primary" onClick={() => run()} title="Execute (F5 / Ctrl+Enter). Runs the selection if any.">
            <IconPlay /> Execute
          </button>
        )}
        <button className="btn" onClick={explain} title="Show the execution plan without running"><IconPlan /> Explain</button>
        {options && options.engine_major >= 5 && (
          <button className="btn" title="Run the statement under the Firebird 5 PSQL profiler (in a separate attachment) and show the report"
            onClick={() => {
              const q = view.current ? selectionOrAll(view.current) : sql;
              if (!q.trim()) return;
              const db = app.servers.find((s) => s.id === props.serverId)?.databases.find((d) => d.id === props.dbId);
              app.openTab({ kind: "profiler", title: `${db?.name ?? "db"} · profile`, serverId: props.serverId, dbId: props.dbId, props: { sql: q, autorun: true } });
            }}>Profile</button>
        )}
        <span className="sep" />
        <button className={"btn" + (autocommit ? " on" : "")} onClick={() => setAutocommit(!autocommit)} title="Commit after every statement">
          Auto-commit {autocommit ? "on" : "off"}
        </button>
        <button className="btn" disabled={!txOpen} onClick={() => txAction("commit")}>Commit</button>
        <button className="btn" disabled={!txOpen} onClick={() => txAction("rollback")}>Rollback</button>
        <div style={{ position: "relative" }}>
          <button className={"btn" + (showTx ? " on" : "")} disabled={!options} onClick={() => setShowTx(!showTx)} title="Transaction isolation, read-only, lock and statement timeouts">
            {txLabel}{options?.read_only ? " · RO" : ""}{options?.statement_timeout ? ` · ${options.statement_timeout} ms` : ""} ▾
          </button>
          {showTx && options && session && (
            <TxSettings options={options} onClose={() => setShowTx(false)} onApply={async (o) => setOptions(await api.setOptions(session, o))} />
          )}
        </div>
        <span className="sep" />
        <label>Rows
          <select className="text" value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))}>
            {[100, 1000, 10000, 100000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}
          </select>
        </label>
        <label className="check" title="Fetch the PLAN of every executed statement"><input type="checkbox" checked={withPlan} onChange={(e) => setWithPlan(e.target.checked)} /> Plans</label>
        <label className="check" title="Measure page and record I/O of every statement (from MON$ tables)"><input type="checkbox" checked={withStats} onChange={(e) => setWithStats(e.target.checked)} /> Stats</label>
        <span className="grow" />
        {aiStatus?.enabled && (
          <div style={{ position: "relative" }}>
            <button className={"btn small" + (aiMenu ? " on" : "")} onClick={() => setAiMenu(!aiMenu)} title={`AI assistant (${aiStatus.provider})`}>✦ Assistant ▾</button>
            {aiMenu && (
              <div className="ctx" style={{ position: "absolute", right: 0, top: "100%" }} onMouseLeave={() => setAiMenu(false)}>
                {AI_TASKS.map(([k, label]) => <button key={k} onClick={() => runAi(k)}>{label}</button>)}
              </div>
            )}
          </div>
        )}
        <button className="btn small" title="Format SQL (Shift+Alt+F)" onClick={format}>Format</button>
        <button className="btn icon" title="Save selection as snippet" onClick={saveSnippet}><IconPlus /></button>
        <button className="btn icon" title="Open .sql file" onClick={openFile}><IconUpload /></button>
        <button className="btn icon" title="Save as .sql file" onClick={() => download("query.sql", sql, "text/sql")}><IconDownload /></button>
      </div>
      <div className="qt-editor" style={{ height: editorH }}>
        <SqlEditor
          value={sql}
          onChange={setSql}
          onExecute={(t) => run(t)}
          onExplain={() => explain()}
          onMacro={(key) => {
            const m = getPrefs().macros.find((x) => x.key === key);
            const v = view.current;
            if (!m || !v) return;
            const sel = v.state.selection.main;
            const selected = sel.empty ? "" : v.state.sliceDoc(sel.from, sel.to);
            run(m.sql.split("$SELECTION$").join(selected));
          }}
          schema={schema}
          extra={extra}
          onFormatError={(m) => app.toast("Cannot format: " + m, "error")}
          placeholder="-- Write SQL here. F5 / Ctrl+Enter runs the selection or the script. :name and ? prompt for values. Ctrl+/ comments, Shift+Alt+F formats."
          editorRef={(v) => (view.current = v)}
        />
      </div>
      <div className="splitter-h" onMouseDown={startDrag} />
      <div className="qt-results">
        <div className="subtabs">
          <button className={pane === "data" ? "active" : ""} onClick={() => setPane("data")}>Data output{selects.length > 1 ? ` (${selects.length})` : ""}</button>
          <button className={pane === "messages" ? "active" : ""} onClick={() => setPane("messages")}>
            Messages {result?.error && <span className="err">●</span>}
          </button>
          <button className={pane === "plan" ? "active" : ""} onClick={() => setPane("plan")}>Plan</button>
          <button className={pane === "history" ? "active" : ""} onClick={() => setPane("history")}>History</button>
          <button className={pane === "snippets" ? "active" : ""} onClick={() => setPane("snippets")}>Snippets</button>
          {aiStatus?.enabled && <button className={pane === "assistant" ? "active" : ""} onClick={() => setPane("assistant")}>Assistant</button>}
        </div>
        <div className="qt-out">
          {pane === "data" && (
            current ? (
              <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
                {selects.length > 1 && (
                  <div className="toolbar">
                    {selects.map((s, i) => (
                      <button key={i} className={"btn small" + (i === activeResult ? " on" : "")} onClick={() => setActiveResult(i)}>
                        Result {i + 1} <span className="muted">(line {s.line})</span>
                      </button>
                    ))}
                  </div>
                )}
                <div style={{ flex: 1, minHeight: 0 }}>
                  <ResultGrid key={`${result?.elapsed_ms}-${activeResult}`} columns={current.columns!} rows={current.rows!}
                    hasMore={!!(isLastSelect && result?.cursor_open)} loadingMore={loadingMore} onLoadMore={loadMore} />
                </div>
              </div>
            ) : (
              <div className="empty-state">{result ? "The script returned no result set." : "Run a query to see results."}</div>
            )
          )}
          {pane === "messages" && (
            <div className="messages">
              {!result && <span className="muted">No messages.</span>}
              {result?.results.map((r) => (
                <div key={r.index} className="stmt">
                  <div className="muted">-- line {r.line} · {r.elapsed_ms} ms</div>
                  <div>{r.sql.length > 300 ? r.sql.slice(0, 300) + "…" : r.sql}</div>
                  <div className={r.kind === "error" ? "err" : "ok"}>
                    {r.kind === "error" ? "ERROR: " : ""}{r.message}
                    {r.truncated ? ` — first ${r.row_count?.toLocaleString()} rows; more available` : ""}
                  </div>
                  {r.stats && <StatsTable stats={r.stats} />}
                </div>
              ))}
              {result?.error && result.results.length === 0 && <div className="err">{result.error.message}</div>}
              {result && <div className="muted" style={{ paddingTop: 8 }}>Total: {result.elapsed_ms} ms</div>}
            </div>
          )}
          {pane === "plan" && (
            plan ? (
              <div>
                <div className="toolbar">
                  <div className="seg">
                    <button className={planView === "graph" ? "on" : ""} onClick={() => setPlanView("graph")} disabled={!plan.detailed}>Diagram</button>
                    <button className={planView === "text" ? "on" : ""} onClick={() => setPlanView("text")}>Text</button>
                    <button className={planView === "diff" ? "on" : ""} onClick={() => setPlanView("diff")} disabled={!pinned}>Compare with pinned</button>
                  </div>
                  <span className="grow" />
                  <button className="btn small" onClick={() => { setPinned(plan); app.toast("Plan pinned — change the query, Explain again and compare", "info"); }}>Pin plan</button>
                  {pinned && <button className="btn small" onClick={() => { setPinned(null); setPlanView("graph"); }}>Unpin</button>}
                </div>
                {planView === "diff" && pinned ? (
                  <PlanDiff left={pinned.detailed ?? pinned.plan ?? ""} right={plan.detailed ?? plan.plan ?? ""}
                    leftTitle={"Pinned: " + (pinned.sql ?? "").slice(0, 80)} rightTitle={"Current: " + (plan.sql ?? "").slice(0, 80)} />
                ) : planView === "graph" && plan.detailed ? (
                  <>{plan.plan && <pre className="plan mono">{plan.plan}</pre>}<PlanGraph text={plan.detailed} /></>
                ) : (
                  <>
                    {plan.plan && <><h3 style={{ margin: "12px 12px 0" }}>Plan</h3><pre className="plan mono">{plan.plan}</pre></>}
                    {plan.detailed && <><h3 style={{ margin: "12px 12px 0" }}>Detailed plan</h3><PlanTree text={plan.detailed} /></>}
                  </>
                )}
              </div>
            ) : <div className="empty-state">Click Explain, or enable Plans and execute.</div>
          )}
          {pane === "history" && (
            <div>
              {history.length === 0 && <div className="pad muted">No history yet.</div>}
              {history.length > 0 && (
                <div className="toolbar"><span className="grow" /><button className="btn small" onClick={() => { save(HISTORY_KEY, []); setHistory([]); }}>Clear history</button></div>
              )}
              {history.map((h, i) => (
                <div key={i} className="history-item" title="Click to load into the editor" onClick={() => setSql(h.sql)}>
                  <span className={h.ok ? "ok" : "err"}>{h.ok ? "✓" : "✗"}</span>{" "}
                  <span className="muted">{new Date(h.at).toLocaleString()} · {h.ms} ms</span>
                  <pre className="mono">{h.sql}</pre>
                </div>
              ))}
            </div>
          )}
          {pane === "assistant" && <AssistantPane state={ai} status={aiStatus} onInsert={insertText} />}
          {pane === "snippets" && (
            <div>
              <div className="toolbar">
                <span className="muted">Click to insert at the cursor. Use + in the toolbar to save the selection.</span>
                <span className="grow" />
                <button className="btn small" onClick={() => download("fbadmin-snippets.json", JSON.stringify(snippets, null, 2), "application/json")}>Export</button>
              </div>
              {snippets.map((s, i) => (
                <div key={i} className="snippet">
                  <div style={{ flex: 1, cursor: "pointer", minWidth: 0 }} onClick={() => insertText(s.sql)}>
                    <b>{s.name}</b>
                    <pre className="mono">{s.sql}</pre>
                  </div>
                  <button className="btn icon small" title="Delete snippet" onClick={() => {
                    const next = snippets.filter((_, j) => j !== i);
                    setSnippets(next);
                    save(SNIPPETS_KEY, next);
                  }}><IconTrash size={13} /></button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="status">
        <span>{session ? `Connected · attachment ${options?.attachment_id ?? "?"}` : "Not connected"}</span>
        {txOpen && <span className="tx">Transaction open — commit or rollback</span>}
        {result?.cursor_open && <span>Result set open</span>}
        {result && <span>{result.results.length} statement(s) · {result.elapsed_ms} ms</span>}
        {current && <span>{current.row_count?.toLocaleString()}{current.truncated ? "+" : ""} rows</span>}
        {result?.error && <span className="err" style={{ cursor: "pointer" }} onClick={() => setPane("messages")}><IconX size={11} /> error at line {result.error.position?.line ?? result.error.line}</span>}
      </div>
      {paramReq && <ParamsDialog names={paramReq.names} onDone={(v) => { setParamReq(null); paramReq.resolve(v); }} />}
    </div>
  );
}

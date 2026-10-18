import { useCallback, useEffect, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import { api } from "../api";
import { errMsg, keys, useApp } from "../context";
import type { ScriptResult } from "../types";
import { ResultGrid, download } from "./ResultGrid";
import { SqlEditor, selectionOrAll } from "./SqlEditor";
import { IconDownload, IconPlan, IconPlay, IconSpinner, IconUpload } from "./Icons";

interface HistoryEntry { sql: string; at: number; ok: boolean; ms: number }
const HISTORY_KEY = "fbadmin.history";

function loadHistory(): HistoryEntry[] {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
  } catch {
    return [];
  }
}
function pushHistory(e: HistoryEntry) {
  try {
    const h = [e, ...loadHistory().filter((x) => x.sql !== e.sql)].slice(0, 200);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(h));
  } catch { /* storage unavailable */ }
}

const DDL_RE = /^\s*(create|alter|drop|recreate|declare|comment|grant|revoke)\b/im;

export function QueryTool(props: { serverId: string; dbId: string; initialSql?: string; autorun?: boolean; visible: boolean }) {
  const app = useApp();
  const [sql, setSql] = useState(props.initialSql ?? "");
  const [session, setSession] = useState<string | null>(null);
  const [result, setResult] = useState<ScriptResult | null>(null);
  const [running, setRunning] = useState(false);
  const [autocommit, setAutocommit] = useState(true);
  const [txOpen, setTxOpen] = useState(false);
  const [maxRows, setMaxRows] = useState(1000);
  const [withPlan, setWithPlan] = useState(false);
  const [pane, setPane] = useState<string>("data");
  const [activeResult, setActiveResult] = useState(0);
  const [plan, setPlan] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>(loadHistory);
  const [schema, setSchema] = useState<Record<string, string[]>>({});
  const [editorH, setEditorH] = useState(260);
  const view = useRef<EditorView | null>(null);
  const sessionRef = useRef<string | null>(null);

  const ensureSession = useCallback(async () => {
    if (sessionRef.current) return sessionRef.current;
    const { session_id } = await api.openSession(props.serverId, props.dbId);
    sessionRef.current = session_id;
    setSession(session_id);
    return session_id;
  }, [props.serverId, props.dbId]);

  useEffect(() => {
    // table names for autocompletion
    Promise.all([api.objects(props.serverId, props.dbId, "tables"), api.objects(props.serverId, props.dbId, "views")])
      .then(([t, v]) => setSchema(Object.fromEntries([...t, ...v].map((o) => [o.name, []]))))
      .catch(() => {});
    if (props.autorun && props.initialSql) run(props.initialSql);
    return () => {
      if (sessionRef.current) api.closeSession(sessionRef.current).catch(() => {});
    };
  }, []);

  const run = async (text?: string) => {
    const q = text ?? (view.current ? selectionOrAll(view.current) : sql);
    if (!q.trim() || running) return;
    setRunning(true);
    setPlan(null);
    const started = performance.now();
    try {
      let sid = await ensureSession();
      let r: ScriptResult;
      try {
        r = await api.runQuery(sid, { sql: q, autocommit, with_plan: withPlan, max_rows: maxRows });
      } catch (e: any) {
        if (e?.status === 404) {
          // session expired: reopen once
          sessionRef.current = null;
          sid = await ensureSession();
          r = await api.runQuery(sid, { sql: q, autocommit, with_plan: withPlan, max_rows: maxRows });
        } else throw e;
      }
      setResult(r);
      setTxOpen(r.transaction_open);
      const selects = r.results.filter((x) => x.kind === "select");
      setActiveResult(Math.max(0, selects.length - 1));
      setPane(r.error ? "messages" : selects.length ? "data" : "messages");
      if (withPlan) {
        const p = r.results.map((x) => x.plan).filter(Boolean).join("\n\n");
        setPlan(p || null);
      }
      const entry = { sql: q, at: Date.now(), ok: !r.error, ms: Math.round(performance.now() - started) };
      pushHistory(entry);
      setHistory(loadHistory());
      if (!r.error && DDL_RE.test(q) && autocommit) app.invalidate(keys.db(props.serverId, props.dbId));
    } catch (e) {
      setResult({ results: [], error: { message: errMsg(e), index: 0, line: 0, sql: q }, elapsed_ms: 0, transaction_open: txOpen });
      setPane("messages");
    } finally {
      setRunning(false);
    }
  };

  const explain = async () => {
    const q = view.current ? selectionOrAll(view.current) : sql;
    try {
      const sid = await ensureSession();
      const r = await api.explain(sid, q);
      setPlan(r.plan ?? "(no plan — statement does not access tables)");
      setPane("plan");
    } catch (e) {
      setPlan("Error: " + errMsg(e));
      setPane("plan");
    }
  };

  const txAction = async (what: "commit" | "rollback") => {
    if (!session) return;
    try {
      await (what === "commit" ? api.commit(session) : api.rollback(session));
      setTxOpen(false);
      app.toast(what === "commit" ? "Transaction committed" : "Transaction rolled back");
      if (what === "commit") app.invalidate(keys.db(props.serverId, props.dbId));
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
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

  // splitter drag
  const startDrag = (e: React.MouseEvent) => {
    const y0 = e.clientY, h0 = editorH;
    const move = (ev: MouseEvent) => setEditorH(Math.max(80, h0 + ev.clientY - y0));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const selects = result?.results.filter((r) => r.kind === "select") ?? [];
  const current = selects[activeResult];

  return (
    <div className="qt" style={{ display: props.visible ? "flex" : "none" }}>
      <div className="toolbar">
        <button className="btn primary" disabled={running} onClick={() => run()} title="Execute (F5 / Ctrl+Enter). Runs the selection if any.">
          {running ? <IconSpinner className="spin" /> : <IconPlay />} Execute
        </button>
        <button className="btn" onClick={explain} title="Show the execution plan without running"><IconPlan /> Explain</button>
        <span className="sep" />
        <button className={"btn" + (autocommit ? " on" : "")} onClick={() => setAutocommit(!autocommit)} title="Commit after every statement">
          Auto-commit {autocommit ? "on" : "off"}
        </button>
        <button className="btn" disabled={!txOpen} onClick={() => txAction("commit")}>Commit</button>
        <button className="btn" disabled={!txOpen} onClick={() => txAction("rollback")}>Rollback</button>
        <span className="sep" />
        <label>Max rows
          <select className="text" value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))}>
            {[100, 1000, 10000, 100000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}
          </select>
        </label>
        <label className="check" title="Fetch the PLAN of every executed statement"><input type="checkbox" checked={withPlan} onChange={(e) => setWithPlan(e.target.checked)} /> Plans</label>
        <span className="grow" />
        <button className="btn icon" title="Open .sql file" onClick={openFile}><IconUpload /></button>
        <button className="btn icon" title="Save as .sql file" onClick={() => download("query.sql", sql, "text/sql")}><IconDownload /></button>
      </div>
      <div className="qt-editor" style={{ height: editorH }}>
        <SqlEditor
          value={sql}
          onChange={setSql}
          onExecute={(t) => run(t)}
          schema={schema}
          placeholder="-- Write SQL here. F5 or Ctrl+Enter runs the selection or the whole script."
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
                  <ResultGrid key={`${result?.elapsed_ms}-${activeResult}`} columns={current.columns!} rows={current.rows!} />
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
                  <div className={r.kind === "error" ? "err" : "ok"}>{r.kind === "error" ? "ERROR: " : ""}{r.message}{r.truncated ? ` — limited to ${maxRows.toLocaleString()} rows` : ""}</div>
                </div>
              ))}
              {result?.error && result.results.length === 0 && <div className="err">{result.error.message}</div>}
              {result && <div className="muted" style={{ paddingTop: 8 }}>Total: {result.elapsed_ms} ms</div>}
            </div>
          )}
          {pane === "plan" && <pre className="plan mono">{plan ?? "Click Explain, or enable Plans and execute."}</pre>}
          {pane === "history" && (
            <div>
              {history.length === 0 && <div className="pad muted">No history yet.</div>}
              {history.map((h, i) => (
                <div key={i} className="history-item" title="Click to load into the editor" onClick={() => setSql(h.sql)}>
                  <span className={h.ok ? "ok" : "err"}>{h.ok ? "✓" : "✗"}</span>{" "}
                  <span className="muted">{new Date(h.at).toLocaleString()} · {h.ms} ms</span>
                  <pre className="mono">{h.sql}</pre>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="status">
        <span>{session ? "Connected" : "Not connected"}</span>
        {txOpen && <span className="tx">Transaction open — commit or rollback</span>}
        {result && <span>{result.results.length} statement(s) · {result.elapsed_ms} ms</span>}
        {current && <span>{current.row_count}{current.truncated ? "+" : ""} rows</span>}
      </div>
    </div>
  );
}

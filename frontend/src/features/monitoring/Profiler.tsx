import { useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import { IconPlay, IconRefresh, IconTrash } from "../../components/Icons";
import { SqlEditor } from "../../components/SqlEditor";
import { fmtNs, fmtNum, fmtTs, monApi } from "./api";
import { t } from "../../i18n";

type Props = { serverId: string; dbId: string; visible: boolean; initialSql?: string; autorun?: boolean };

export function ProfilerTab(props: Props) {
  const app = useApp();
  const [ov, setOv] = useState<Record<string, any> | null>(null);
  const [mode, setMode] = useState<"run" | "attach">("run");
  const [sql, setSql] = useState(props.initialSql ?? "");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const [lastRun, setLastRun] = useState<Record<string, any> | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [profiling, setProfiling] = useState<Record<number, number>>({}); // attachment -> profile id

  const load = () => monApi.profiler(props.serverId, props.dbId).then(setOv).catch((e) => app.toast(errMsg(e), "error"));
  useEffect(() => {
    load();
    if (props.autorun && props.initialSql) run(props.initialSql); // opened from the Query Tool's Profile button
  }, []);

  const run = async (text?: string) => {
    const q = (text ?? sql).trim();
    if (!q) return;
    setBusy(true);
    try {
      const r = await monApi.profileRun(props.serverId, props.dbId, q, desc || q.slice(0, 60));
      setLastRun(r);
      setSelected(r.profile_id);
      if (r.error) app.toast(r.error, "error");
      load();
    } catch (e) { app.toast(errMsg(e), "error"); } finally { setBusy(false); }
  };
  const start = async (att: number) => {
    try {
      const r = await monApi.profileStart(props.serverId, props.dbId, att, desc || `attachment ${att}`);
      setProfiling((p) => ({ ...p, [att]: r.profile_id }));
      app.toast(t("Profiling attachment {att} (session {id}). Run statements there, then Finish.", { att, id: r.profile_id }));
    } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const finish = async (att: number) => {
    try {
      await monApi.profileFinish(props.serverId, props.dbId, att);
      const pid = profiling[att];
      setProfiling((p) => { const c = { ...p }; delete c[att]; return c; });
      await load();
      if (pid) setSelected(pid);
    } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const del = async (id?: number) => {
    try { await monApi.profileDelete(props.serverId, props.dbId, id); if (id === undefined || id === selected) setSelected(null); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };

  if (ov && !ov.available) {
    return <div className="pad"><h2>{t("Profiler")}</h2><div className="muted">{ov.reason}</div>
      <p className="muted">{t("The PSQL/SQL profiler (RDB$PROFILER package with the Default_Profiler plugin) was introduced in Firebird 5.0.")}</p></div>;
  }
  return (
    <div className="mon">
      <div className="toolbar">
        <div className="seg">
          <button className={mode === "run" ? "on" : ""} onClick={() => setMode("run")}>{t("Run a statement")}</button>
          <button className={mode === "attach" ? "on" : ""} onClick={() => setMode("attach")}>{t("Profile a session")}</button>
        </div>
        <input className="text" placeholder={t("Description (optional)")} value={desc} onChange={(e) => setDesc(e.target.value)} style={{ width: 220 }} />
        {mode === "run" && <button className="btn primary" disabled={busy || !sql.trim()} onClick={() => run()}><IconPlay /> {busy ? t("Profiling…") : t("Profile (F5)")}</button>}
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        <span className="muted">{t("Statements run in a separate attachment and are committed.")}</span>
      </div>
      <div className="mon-body">
        {mode === "run" ? (
          <div style={{ height: 150, border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden", marginBottom: 10 }}>
            <SqlEditor value={sql} onChange={setSql} onExecute={(text) => run(text)} placeholder={t("SELECT * FROM MY_PROCEDURE(...);  -- statements separated by ;")} />
          </div>
        ) : (
          <div style={{ marginBottom: 10 }}>
            <p className="muted" style={{ marginTop: 0 }}>{t("Start profiling another attachment — for example a Query Tool tab (its attachment id is shown in its status bar) — run statements there, then press Finish to flush the profile.")}</p>
            <div className="grid-wrap">
              <table className="grid">
                <thead><tr><th>{t("Attachment")}</th><th>{t("User")}</th><th>{t("Process")}</th><th>{t("Address")}</th><th /></tr></thead>
                <tbody>
                  {(ov?.attachments ?? []).map((a: any) => (
                    <tr key={a.id}>
                      <td>{a.id}{a.query_tool ? " " + t("(fbadmin Query Tool)") : ""}</td><td>{a.username}</td>
                      <td>{(a.remote_process ?? "").split(/[\\/]/).pop()}</td><td>{a.remote_address}</td>
                      <td>{profiling[a.id] ? <button className="btn small primary" onClick={() => finish(a.id)}>{t("Finish & show (session {id})", { id: profiling[a.id] })}</button>
                        : <button className="btn small" onClick={() => start(a.id)}>{t("Start profiling")}</button>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        {lastRun && lastRun.profile_id === selected && <RunSummary run={lastRun} />}
        <div className="split2" style={{ gridTemplateColumns: "260px 1fr" }}>
          <div>
            <h3 style={{ marginTop: 0, display: "flex" }}>{t("Sessions")} <span style={{ flex: 1 }} />{(ov?.sessions ?? []).length > 0 && <button className="btn small danger" onClick={() => del()}>{t("Delete all")}</button>}</h3>
            {(ov?.sessions ?? []).length === 0 && <div className="muted">{t("No profile yet.")}</div>}
            {(ov?.sessions ?? []).map((s: any) => (
              <div key={s.profile_id} className={"tree-row" + (s.profile_id === selected ? " selected" : "")} style={{ height: "auto", padding: "4px 6px", cursor: "pointer" }} onClick={() => setSelected(s.profile_id)}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="label"><b>#{s.profile_id}</b> {s.description}</div>
                  <div className="muted" style={{ fontSize: 11 }}>{t("{time} · att {id} · {duration}", { time: fmtTs(s.started_at), id: s.attachment_id, duration: s.finished_at ? fmtNs(s.total_ns) : t("running") })}</div>
                </div>
                <button className="btn icon small" title={t("Delete")} onClick={(e) => { e.stopPropagation(); del(s.profile_id); }}><IconTrash size={12} /></button>
              </div>
            ))}
          </div>
          <div style={{ minWidth: 0 }}>{selected !== null ? <Report key={selected} {...props} id={selected} /> : <div className="muted">{t("Run a statement or select a session.")}</div>}</div>
        </div>
      </div>
    </div>
  );
}

function RunSummary(props: { run: Record<string, any> }) {
  const r = props.run;
  return (
    <div className="muted" style={{ marginBottom: 10 }}>
      {r.error ? <span className="err">{r.error}</span> : <>{t("Executed {n} statement(s) in {ms} ms.", { n: r.results.length, ms: r.elapsed_ms })} </>}
      {r.results.filter((x: any) => x.columns.length).slice(0, 1).map((x: any, i: number) => (
        <span key={i}> {t("First result:")} {x.rows.slice(0, 3).map((row: any[]) => row.join(", ")).join(" | ")}{x.rows.length > 3 ? " …" : ""}</span>
      ))}
    </div>
  );
}

interface Stmt {
  statement_id: number; parent_id: number | null; statement_type: string; package_name: string | null; routine_name: string | null;
  sql_text: string | null; calls: number | null; total_ns: number | null; children: number[]; calls_to: number[];
}

const stmtName = (s: Stmt) => s.routine_name ? `${s.statement_type.toLowerCase()} ${s.package_name ? s.package_name + "." : ""}${s.routine_name}`
  : `${s.statement_type.toLowerCase()}: ${(s.sql_text ?? "").replace(/\s+/g, " ").slice(0, 90)}`;

function Report(props: Props & { id: number }) {
  const [rep, setRep] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  useEffect(() => { monApi.profileReport(props.serverId, props.dbId, props.id).then((r) => { setRep(r); setError(null); }).catch((e) => setError(errMsg(e))); }, [props.id]);
  const byId = useMemo(() => Object.fromEntries((rep?.statements ?? []).map((s: Stmt) => [s.statement_id, s])) as Record<number, Stmt>, [rep]);
  if (error) return <div className="err">{error}</div>;
  if (!rep) return <div className="muted">{t("Loading…")}</div>;
  const total = rep.total_ns || 1;
  const rows: { s: Stmt; depth: number; via: string }[] = [];
  const walk = (id: number, depth: number, via: string, seen: Set<number>) => {
    const s = byId[id];
    if (!s || seen.has(id)) return;
    rows.push({ s, depth, via });
    const next = new Set(seen).add(id);
    s.children.forEach((c) => walk(c, depth + 1, "", next));
    s.calls_to.forEach((c) => walk(c, depth + 1, t("calls") + " ", next));
  };
  rep.roots.forEach((r: number) => walk(r, 0, "", new Set()));
  const selStmt = sel !== null ? byId[sel] : null;
  const hot = [...rep.lines].sort((a: any, b: any) => (b.total_ns ?? 0) - (a.total_ns ?? 0)).slice(0, 12);
  return (
    <>
      <div className="cards">
        <div className="card"><div className="k">{t("Total time")}</div><div className="v">{fmtNs(rep.total_ns)}</div></div>
        <div className="card"><div className="k">{t("Statements")}</div><div className="v">{rep.statements.length}</div></div>
        <div className="card"><div className="k">{t("PSQL lines")}</div><div className="v">{rep.lines.length}</div></div>
        <div className="card"><div className="k">{t("Record sources")}</div><div className="v">{rep.record_sources.length}</div></div>
      </div>
      <h3>{t("Call tree")}</h3>
      <div className="prof-tree">
        <div className="node" style={{ cursor: "default", fontWeight: 600, fontFamily: "inherit" }}><span>{t("Statement / routine")}</span><span className="num">{t("Time")}</span><span className="num">{t("Calls")}</span><span>{t("Share of total")}</span></div>
        {rows.map(({ s, depth, via }, i) => (
          <div key={i} className={"node" + (sel === s.statement_id ? " sel" : "")} onClick={() => setSel(s.statement_id)} title={s.sql_text ?? ""}>
            <span className="name" style={{ paddingLeft: depth * 16 }}>{depth ? "└ " : ""}{via}{stmtName(s)}</span>
            <span className="num">{fmtNs(s.total_ns)}</span>
            <span className="num">{fmtNum(s.calls)}</span>
            <span className="share" title={`${(((s.total_ns ?? 0) / total) * 100).toFixed(1)} %`}><div style={{ width: `${Math.min(100, ((s.total_ns ?? 0) / total) * 100)}%` }} /></span>
          </div>
        ))}
      </div>
      {selStmt ? <StmtDetail rep={rep} s={selStmt} /> : (
        <>
          <h3>{t("PSQL hot spots")}</h3>
          {hot.length === 0 ? <div className="muted">{t("No PSQL executed (only plain SQL): select a statement to see its record sources.")}</div> : <LineTable lines={hot} showRoutine />}
        </>
      )}
    </>
  );
}

function LineTable(props: { lines: any[]; showRoutine?: boolean }) {
  return (
    <div className="grid-wrap">
      <table className="grid">
        <thead><tr>{props.showRoutine && <th>{t("Routine")}</th>}<th>{t("Line")}</th><th>{t("Col")}</th><th>{t("Executions")}</th><th>{t("Total")}</th><th>{t("Avg")}</th><th>{t("Max")}</th></tr></thead>
        <tbody>
          {props.lines.map((l: any, i: number) => (
            <tr key={i}>{props.showRoutine && <td>{l.package_name ? l.package_name + "." : ""}{l.routine_name ?? `(${l.statement_type?.toLowerCase()})`}</td>}
              <td className="num">{l.line_num}</td><td className="num">{l.column_num}</td><td className="num">{fmtNum(l.counter)}</td>
              <td className="num">{fmtNs(l.total_ns)}</td><td className="num">{fmtNs(l.avg_ns)}</td><td className="num">{fmtNs(l.max_ns)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StmtDetail(props: { rep: Record<string, any>; s: Stmt }) {
  const { rep, s } = props;
  const lines = rep.lines.filter((l: any) => l.statement_id === s.statement_id);
  const routine = rep.routines.find((r: any) => r.routine_name === s.routine_name && (r.package_name ?? null) === (s.package_name ?? null));
  const sources = rep.record_sources.filter((r: any) => r.statement_id === s.statement_id);
  return (
    <>
      <h3>{stmtName(s)}</h3>
      {s.sql_text && <div className="code-block">{s.sql_text}</div>}
      {routine?.source && lines.length > 0 && <SourceView source={routine.source} offset={routine.line_offset} lines={lines} />}
      {lines.length > 0 && (<><h3>{t("Per-line timings")}</h3><LineTable lines={lines} /></>)}
      {sources.length > 0 && (
        <>
          <h3>{t("Record sources (access paths)")}</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>{t("Cursor")}</th><th>{t("Access path")}</th><th>{t("Opens")}</th><th>{t("Fetches")}</th><th>{t("Open time")}</th><th>{t("Fetch time")}</th><th>{t("Total")}</th></tr></thead>
              <tbody>
                {sources.map((r: any, i: number) => (
                  <tr key={i}>
                    <td>{r.cursor_name ?? r.cursor_id}{r.cursor_line ? ` @${r.cursor_line}` : ""}</td>
                    <td className="mono" style={{ whiteSpace: "pre", paddingLeft: 8 + r.depth * 14 }}>{(r.access_path ?? "").replace(/^-> /, "")}</td>
                    <td className="num">{fmtNum(r.open_counter)}</td><td className="num">{fmtNum(r.fetch_counter)}</td>
                    <td className="num">{fmtNs(r.open_total_ns)}</td><td className="num">{fmtNs(r.fetch_total_ns)}</td><td className="num">{fmtNs(r.total_ns)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {!lines.length && !sources.length && <div className="muted">{t("No line or record source statistics for this statement.")}</div>}
    </>
  );
}

function SourceView(props: { source: string; offset: number; lines: any[] }) {
  const src = props.source.split("\n");
  const byLine: Record<number, { counter: number; total: number }> = {};
  for (const l of props.lines) {
    const k = l.line_num - props.offset;
    const e = (byLine[k] ??= { counter: 0, total: 0 });
    e.counter += l.counter ?? 0;
    e.total += l.total_ns ?? 0;
  }
  const max = Math.max(1, ...Object.values(byLine).map((e) => e.total));
  return (
    <>
      <h3>{t("Source with per-line time")} <span className="muted" style={{ textTransform: "none", letterSpacing: 0, fontWeight: 400 }}>{t("(line numbers of the CREATE statement = stored source + {offset})", { offset: props.offset })}</span></h3>
      <div className="src">
        <div className="ln" style={{ fontWeight: 600, fontFamily: "inherit" }}><span className="no">{t("Line")}</span><span className="c">{t("Count")}</span><span className="t">{t("Time")}</span><span /></div>
        {src.map((text, i) => {
          const e = byLine[i + 1];
          return (
            <div key={i} className={"ln" + (e && e.total / max > 0.25 ? " hot" : "")}>
              <span className="no">{i + 1 + props.offset}</span><span className="c">{e ? fmtNum(e.counter) : ""}</span><span className="t">{e ? fmtNs(e.total) : ""}</span>
              <span className="code">{e && <span className="heat" style={{ width: `${(e.total / max) * 100}%` }} />}{text}</span>
            </div>
          );
        })}
      </div>
    </>
  );
}

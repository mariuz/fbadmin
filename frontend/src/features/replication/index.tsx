/** Milestone 0.8 - replication (Firebird 4+): publication, replica mode, lag, replication.conf helper. */
import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import { N_, t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconCopy, IconRefresh } from "../../components/Icons";
import { Modal } from "../../components/Modal";
import "../monitoring/monitoring.css";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/replication`;
interface JournalEvent { time: string | null; segment: number | null; error: boolean; text: string }
interface Journal {
  replica_mode: string; replication_sequence: number | null; rate_per_hour: number | null;
  history: Array<{ t: number; seq: number }>; events: JournalEvent[]; last_segment: number | null;
  errors: number; last_error: JournalEvent | null;
}

function fmtDuration(sec: number): string {
  if (sec < 90) return t("{n} s", { n: Math.round(sec) });
  if (sec < 5400) return t("{n} min", { n: Math.round(sec / 60) });
  return t("{n} h", { n: (sec / 3600).toFixed(1) });
}

/** Journal tracking: sequence history/rate (sampled while the tab is open) and firebird.log segment events. */
function JournalPanel(props: { serverId: string; dbId: string; visible: boolean; tick: number }) {
  const [j, setJ] = useState<Journal | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!props.visible) return;
    repApi.journal(props.serverId, props.dbId).then((r) => { setJ(r); setError(null); }).catch((e) => setError(errMsg(e)));
  }, [props.visible, props.tick]);
  if (error) return <div className="err">{error}</div>;
  if (!j) return null;
  const replica = j.replica_mode !== "none";
  const pts = j.history;
  const w = 320, h = 48;
  let path = "";
  if (pts.length > 1) {
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t || t0 + 1, s0 = pts[0].seq, s1 = Math.max(pts[pts.length - 1].seq, s0 + 1);
    path = pts.map((p, i) => `${i ? "L" : "M"}${((p.t - t0) / Math.max(1, t1 - t0)) * w},${h - ((p.seq - s0) / (s1 - s0)) * h}`).join(" ");
  }
  return (
    <>
      <div className="cards">
        <div className="card"><div className="k">{replica ? t("Last applied segment") : t("Current journal segment")}</div><div className="v">{j.replication_sequence ?? "–"}</div></div>
        <div className="card"><div className="k">{replica ? t("Applied per hour") : t("Segments per hour")}</div><div className="v">{j.rate_per_hour == null ? "–" : j.rate_per_hour.toFixed(1)}</div></div>
        <div className="card"><div className="k">{t("Last segment in firebird.log")}</div><div className="v">{j.last_segment ?? "–"}</div></div>
        <div className="card"><div className="k">{t("Replication errors in log")}</div><div className={"v" + (j.errors ? " warn" : "")}>{j.errors}</div></div>
      </div>
      {pts.length > 1 && (
        <svg width={w} height={h + 4} style={{ display: "block", margin: "6px 0" }} aria-label={t("Replication sequence over time")}>
          <path d={path} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
        </svg>
      )}
      {j.last_error && (
        <div className="alerts"><div className="alert critical"><span className="lvl">{t("error")}</span><span>{j.last_error.time ? j.last_error.time.replace("T", " ") + " · " : ""}{j.last_error.text}</span></div></div>
      )}
      {j.events.length > 0 && (
        <div className="grid-wrap" style={{ maxHeight: 220, marginTop: 6 }}>
          <table className="grid">
            <thead><tr><th>{t("Time")}</th><th>{t("Segment")}</th><th>{t("Event")}</th></tr></thead>
            <tbody>{j.events.slice(-50).reverse().map((ev, i) => (
              <tr key={i}><td>{ev.time?.replace("T", " ") ?? "–"}</td><td>{ev.segment ?? "–"}</td><td className={ev.error ? "warn" : ""}>{ev.text}</td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <p className="muted">{t("Journal and archive segments are files on the server and cannot be listed remotely. The sequence is sampled while this tab is open; set verbose_logging = true in replication.conf to see every applied segment in firebird.log.")}</p>
    </>
  );
}

const repApi = {
  status: (sid: string, dbid: string, peer?: { server: string; db: string }) =>
    request<Record<string, any>>("GET", base(sid, dbid) + (peer ? `?peer_server=${peer.server}&peer_db=${peer.db}` : "")),
  publication: (sid: string, dbid: string, enabled: boolean) =>
    request<{ sql: string; status: Record<string, any> }>("POST", base(sid, dbid) + "/publication", { enabled }),
  tables: (sid: string, dbid: string, action: "include" | "exclude", tables: string[], all = false) =>
    request<{ sql: string; status: Record<string, any> }>("POST", base(sid, dbid) + "/tables", { action, tables, all }),
  replicaMode: (sid: string, dbid: string, mode: string) =>
    request<{ status: Record<string, any> }>("POST", base(sid, dbid) + "/replica-mode", { mode }),
  log: (sid: string, dbid: string) => request<{ entries: string[]; total_entries: number }>("GET", base(sid, dbid) + "/log"),
  journal: (sid: string, dbid: string) => request<Journal>("GET", base(sid, dbid) + "/journal"),
  conf: (body: Record<string, unknown>) => request<{ text: string; errors: string[]; notes: string[] }>("POST", "/api/replication/conf", body),
};

const MODE_LABEL: Record<string, string> = { none: N_("Not a replica (primary / standalone)"), read_only: N_("Replica, read-only"), read_write: N_("Replica, read-write") };

/** Translate-then-split: replaces {key} placeholders in an already translated text with React nodes. */
function rich(text: string, parts: Record<string, ReactNode>): ReactNode[] {
  return text.split(/(\{\w+\})/).map((seg, i) => {
    const m = /^\{(\w+)\}$/.exec(seg);
    return m && m[1] in parts ? <Fragment key={i}>{parts[m[1]]}</Fragment> : seg;
  });
}

function ReplicationTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const [st, setSt] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [peer, setPeer] = useState<string>("");
  const [log, setLog] = useState<string[] | null>(null);
  const peerObj = peer ? { server: peer.split("/")[0], db: peer.split("/")[1] } : undefined;

  const [tick, setTick] = useState(0);
  const load = () => repApi.status(props.serverId, props.dbId, peerObj).then((s) => { setSt(s); setError(null); setTick((n) => n + 1); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, [peer]);
  useEffect(() => {
    if (!props.visible) return;
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [props.visible, peer]);

  const act = async (fn: () => Promise<{ sql?: string; status: Record<string, any> }>) => {
    try { const r = await fn(); setSt((old) => ({ ...r.status, peer: old?.peer })); setChecked(new Set()); app.toast(r.sql ?? t("Done")); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  const confirmMode = (mode: string) => app.showDialog(
    <Modal title={t("Change replica mode")} onClose={() => app.showDialog(null)} footer={<>
      <button className="btn" onClick={() => app.showDialog(null)}>{t("Cancel")}</button>
      <button className="btn primary" onClick={() => { app.showDialog(null); act(() => repApi.replicaMode(props.serverId, props.dbId, mode)); }}>{t("Switch to {mode}", { mode: mode.replace("_", "-") })}</button>
    </>}>
      <p>{rich(t("Set the replica mode of this database to {label} (same as {command})."), { label: <b>{t(MODE_LABEL[mode])}</b>, command: <code>gfix -replica {mode}</code> })}</p>
      <p className="muted">{t("This needs exclusive access: fbadmin closes its own connections to the database (including Query Tool tabs); it fails if other applications are attached. A read-only replica rejects changes from normal users; only the replication applier may write.")}</p>
    </Modal>,
  );
  const allDbs = app.servers.flatMap((s) => s.databases.filter((d) => !(s.id === props.serverId && d.id === props.dbId)).map((d) => ({ key: `${s.id}/${d.id}`, label: `${s.name} / ${d.name}` })));

  return (
    <div className="mon">
      <div className="toolbar">
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        {error && <span className="err">{error}</span>}
      </div>
      <div className="mon-body">
        {st && !st.supported && (
          <div className="alerts"><div className="alert warning"><span className="lvl">{t("n/a")}</span><span>{t("Built-in replication needs Firebird 4.0 or newer; this server runs {version}. The replication.conf helper below still works for planning.", { version: st.engine_version })}</span></div></div>
        )}
        {st?.supported && (
          <>
            <div className="cards">
              <div className="card"><div className="k">{t("Replica mode")}</div><div className="v" style={{ fontSize: 16 }}>{st.replica_mode.replace("_", "-")}</div></div>
              <div className="card"><div className="k">{t("Publication")}</div><div className="v" style={{ fontSize: 16 }}>{st.publication_enabled ? t("enabled") : t("disabled")}</div></div>
              <div className="card"><div className="k">{t("Published tables")}</div><div className="v">{st.tables.filter((x: any) => x.published).length}<span className="muted" style={{ fontSize: 13 }}> / {st.tables.length}</span></div></div>
              <div className="card"><div className="k">{t("Replication sequence")}</div><div className="v">{st.replication_sequence ?? "–"}</div></div>
              <div className="card"><div className="k">{t("New tables")}</div><div className="v" style={{ fontSize: 16 }}>{st.auto_enable ? t("auto-published") : t("manual")}</div></div>
            </div>
            {st.warnings.length > 0 && <div className="alerts">{st.warnings.map((w: string, i: number) => <div key={i} className="alert warning"><span className="lvl">{t("warning")}</span><span>{w}</span></div>)}</div>}
            <div className="muted" style={{ fontSize: 12 }}>{t("Database GUID {guid}", { guid: st.guid })} · {st.database_name}</div>

            <h3>{t("Publication (primary side)")}</h3>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              {st.publication_enabled
                ? <button className="btn" onClick={() => act(() => repApi.publication(props.serverId, props.dbId, false))}>{t("Disable publication")}</button>
                : <button className="btn primary" onClick={() => act(() => repApi.publication(props.serverId, props.dbId, true))}>{t("Enable publication")}</button>}
              <span className="sep" style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />
              <button className="btn" disabled={!checked.size} onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "include", [...checked]))}>{t("Include selected")}</button>
              <button className="btn" disabled={!checked.size} onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "exclude", [...checked]))}>{t("Exclude selected")}</button>
              <button className="btn" onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "include", [], true))}>{t("Include all (and future tables)")}</button>
              <button className="btn" onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "exclude", [], true))}>{t("Exclude all")}</button>
            </div>
            <div className="grid-wrap" style={{ marginTop: 8, maxHeight: 320 }}>
              <table className="grid">
                <thead><tr><th style={{ width: 30 }}><input type="checkbox" checked={checked.size > 0 && checked.size === st.tables.length} onChange={(e) => setChecked(e.target.checked ? new Set(st.tables.map((x: any) => x.name)) : new Set())} /></th><th>{t("Table")}</th><th>{t("Published")}</th><th>{t("Primary/unique key")}</th></tr></thead>
                <tbody>
                  {st.tables.map((tb: any) => (
                    <tr key={tb.name}>
                      <td><input type="checkbox" checked={checked.has(tb.name)} onChange={(e) => setChecked((c) => { const n = new Set(c); e.target.checked ? n.add(tb.name) : n.delete(tb.name); return n; })} /></td>
                      <td>{tb.name}</td><td className={tb.published ? "ok" : "muted"}>{tb.published ? "✓ " + t("yes") : t("no")}</td>
                      <td className={tb.has_key ? "" : "warn"}>{tb.has_key ? t("yes") : "▲ " + t("no (uses DB_KEY)")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3>{t("Replica mode")}</h3>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <div className="seg">
                {["none", "read_only", "read_write"].map((m) => (
                  <button key={m} className={st.replica_mode === m ? "on" : ""} disabled={st.replica_mode === m} onClick={() => confirmMode(m)}>{m.replace("_", "-")}</button>
                ))}
              </div>
              <span className="muted">{MODE_LABEL[st.replica_mode] ? t(MODE_LABEL[st.replica_mode]) : undefined}</span>
            </div>

            <h3>{t("Status and lag")}</h3>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label className="muted">{t("Compare with the other side:")}</label>
              <select className="text" value={peer} onChange={(e) => setPeer(e.target.value)}>
                <option value="">{t("— choose the primary/replica database —")}</option>
                {allDbs.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
              </select>
            </div>
            {st.peer && (st.peer.error ? <div className="err">{st.peer.error}</div> : (
              <table className="kv" style={{ marginTop: 8 }}><tbody>
                <tr><td>{t("Peer replica mode")}</td><td>{st.peer.replica_mode}</td></tr>
                <tr><td>{t("Peer replication sequence")}</td><td>{st.peer.replication_sequence ?? "–"}</td></tr>
                <tr><td>{t("Segments behind (approx.)")}</td><td className={st.peer.segments_behind > 1 ? "warn" : ""}>{st.peer.segments_behind ?? "–"}</td></tr>
                <tr><td>{t("Estimated catch-up time")}</td><td>{st.peer.catch_up_seconds == null ? t("unknown (needs a few samples of the replica)") : fmtDuration(st.peer.catch_up_seconds)}</td></tr>
                <tr><td>{t("Peer GUID")}</td><td>{st.peer.guid}</td></tr>
              </tbody></table>
            ))}
            <p className="muted">{t("Lag is approximated from RDB$GET_CONTEXT('SYSTEM', 'REPLICATION_SEQUENCE') on both sides (journal segment numbers). Exact apply status is only in the server's replication.log.")}</p>
            <h3>{t("Journal")}</h3>
            <JournalPanel serverId={props.serverId} dbId={props.dbId} visible={props.visible} tick={tick} />
            <button className="btn" onClick={() => repApi.log(props.serverId, props.dbId).then((r) => setLog(r.entries)).catch((e) => app.toast(errMsg(e), "error"))}>{t("Show replication entries of firebird.log")}</button>
            {log && (log.length ? <div className="code-block" style={{ maxHeight: 260, overflow: "auto" }}>{log.join("\n\n")}</div> : <div className="muted">{t("No replication-related entries in firebird.log.")}</div>)}
          </>
        )}
        <ConfHelper dbPath={st?.database_name} />
      </div>
    </div>
  );
}

const FIELDS: Record<string, [string, string, string][]> = {
  primary: [
    ["journal_directory", N_("Journal directory"), "/var/lib/firebird/journal"],
    ["journal_archive_directory", N_("Journal archive directory"), "/var/lib/firebird/archive"],
    ["journal_archive_timeout", N_("Archive timeout (s)"), "60"],
    ["journal_segment_size", N_("Segment size"), "16M"],
    ["journal_segment_count", N_("Segment count"), "8"],
    ["journal_archive_command", N_("Archive command (optional)"), "cp $(pathname) $(archivepathname)"],
    ["include_filter", N_("Include filter (regex)"), ""],
    ["exclude_filter", N_("Exclude filter (regex)"), ""],
  ],
  primary_sync: [["sync_replica", N_("Replica connection"), "SYSDBA:password@replica-host:/data/replica.fdb"]],
  replica: [
    ["journal_source_directory", N_("Journal source directory"), "/var/lib/firebird/archive"],
    ["source_guid", N_("Source GUID (optional)"), "{...}"],
    ["apply_idle_timeout", N_("Apply idle timeout (s)"), "10"],
    ["apply_error_timeout", N_("Apply error timeout (s)"), "60"],
  ],
};

function ConfHelper(props: { dbPath?: string }) {
  const app = useApp();
  const [role, setRole] = useState("primary");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [flags, setFlags] = useState({ verbose_logging: false, log_errors: true, disable_on_error: false });
  const [out, setOut] = useState<{ text: string; errors: string[]; notes: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const body = useMemo(() => {
    const b: Record<string, unknown> = { role, database: vals.database ?? props.dbPath ?? "" };
    for (const [k] of FIELDS[role]) if (vals[k]) b[k] = vals[k];
    if (flags.verbose_logging) b.verbose_logging = true;
    if (role !== "replica") { b.log_errors = flags.log_errors; if (flags.disable_on_error) b.disable_on_error = true; }
    return b;
  }, [role, vals, flags, props.dbPath]);
  useEffect(() => {
    const timer = setTimeout(() => repApi.conf(body).then((r) => { setOut(r); setError(null); }).catch((e) => setError(errMsg(e))), 250);
    return () => clearTimeout(timer);
  }, [body]);
  const set = (k: string, v: string) => setVals((x) => ({ ...x, [k]: v }));
  return (
    <>
      <h3>{t("replication.conf helper")}</h3>
      <p className="muted" style={{ marginTop: 0 }}>{rich(t("Generates a {block} block for {file} in the Firebird root directory of the server (not readable remotely). The configuration is read when the database is opened by its first attachment."), { block: <code>database = …</code>, file: <code>replication.conf</code> })}</p>
      <div className="split2">
        <div className="form-grid" style={{ gridTemplateColumns: "180px 1fr" }}>
          <label>{t("Role")}</label>
          <select className="text" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="primary">{t("Primary — asynchronous (journal files)")}</option>
            <option value="primary_sync">{t("Primary — synchronous (direct to replica)")}</option>
            <option value="replica">{t("Replica — applies archived journals")}</option>
          </select>
          <label>{t("Database (path or alias)")}</label>
          <input className="text" value={vals.database ?? props.dbPath ?? ""} onChange={(e) => set("database", e.target.value)} />
          {FIELDS[role].map(([k, label, ph]) => (
            <Fragment key={k}><label>{t(label)}</label><input className="text" placeholder={ph} value={vals[k] ?? ""} onChange={(e) => set(k, e.target.value)} /></Fragment>
          ))}
          <label>{t("Logging")}</label>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <label className="check"><input type="checkbox" checked={flags.verbose_logging} onChange={(e) => setFlags({ ...flags, verbose_logging: e.target.checked })} /> verbose_logging</label>
            {role !== "replica" && <label className="check"><input type="checkbox" checked={flags.log_errors} onChange={(e) => setFlags({ ...flags, log_errors: e.target.checked })} /> log_errors</label>}
            {role !== "replica" && <label className="check"><input type="checkbox" checked={flags.disable_on_error} onChange={(e) => setFlags({ ...flags, disable_on_error: e.target.checked })} /> disable_on_error</label>}
          </div>
        </div>
        <div>
          {error && <div className="err">{error}</div>}
          {out && (
            <>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button className="btn small" onClick={() => { navigator.clipboard?.writeText(out.text); app.toast(t("Copied")); }}><IconCopy size={12} /> {t("Copy")}</button>
              </div>
              <div className="code-block">{out.text}</div>
              {out.errors.map((e, i) => <div key={i} className="warn">▲ {e}</div>)}
              {out.notes.map((n, i) => <div key={i} className="muted">• {n}</div>)}
            </>
          )}
        </div>
      </div>
    </>
  );
}

export function openReplication(app: AppApi, sid: string, dbid: string) {
  const name = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? dbid;
  app.openTab({ kind: "replication", title: t("{name} · replication", { name }), serverId: sid, dbId: dbid });
}

registerTabKind("replication", {
  icon: <span style={{ fontSize: 12 }}>⇄</span>,
  render: (tab, visible) => <ReplicationTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app, sel) => (sel.kind === "database" && sel.dbId
  ? [{ label: t("Replication"), onClick: () => openReplication(app, sel.serverId, sel.dbId!) }]
  : []));

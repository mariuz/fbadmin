/** Milestone 0.8 - replication (Firebird 4+): publication, replica mode, lag, replication.conf helper. */
import { Fragment, useEffect, useMemo, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import { registerMenu, registerTabKind } from "../../registry";
import { IconCopy, IconRefresh } from "../../components/Icons";
import { Modal } from "../../components/Modal";
import "../monitoring/monitoring.css";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/replication`;
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
  conf: (body: Record<string, unknown>) => request<{ text: string; errors: string[]; notes: string[] }>("POST", "/api/replication/conf", body),
};

const MODE_LABEL: Record<string, string> = { none: "Not a replica (primary / standalone)", read_only: "Replica, read-only", read_write: "Replica, read-write" };

function ReplicationTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const [st, setSt] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [peer, setPeer] = useState<string>("");
  const [log, setLog] = useState<string[] | null>(null);
  const peerObj = peer ? { server: peer.split("/")[0], db: peer.split("/")[1] } : undefined;

  const load = () => repApi.status(props.serverId, props.dbId, peerObj).then((s) => { setSt(s); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, [peer]);
  useEffect(() => {
    if (!props.visible) return;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [props.visible, peer]);

  const act = async (fn: () => Promise<{ sql?: string; status: Record<string, any> }>) => {
    try { const r = await fn(); setSt((old) => ({ ...r.status, peer: old?.peer })); setChecked(new Set()); app.toast(r.sql ?? "Done"); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  const confirmMode = (mode: string) => app.showDialog(
    <Modal title="Change replica mode" onClose={() => app.showDialog(null)} footer={<>
      <button className="btn" onClick={() => app.showDialog(null)}>Cancel</button>
      <button className="btn primary" onClick={() => { app.showDialog(null); act(() => repApi.replicaMode(props.serverId, props.dbId, mode)); }}>Switch to {mode.replace("_", "-")}</button>
    </>}>
      <p>Set the replica mode of this database to <b>{MODE_LABEL[mode]}</b> (same as <code>gfix -replica {mode}</code>).</p>
      <p className="muted">This needs exclusive access: fbadmin closes its own connections to the database (including Query Tool tabs); it fails if other applications are attached.
        A read-only replica rejects changes from normal users; only the replication applier may write.</p>
    </Modal>,
  );
  const allDbs = app.servers.flatMap((s) => s.databases.filter((d) => !(s.id === props.serverId && d.id === props.dbId)).map((d) => ({ key: `${s.id}/${d.id}`, label: `${s.name} / ${d.name}` })));

  return (
    <div className="mon">
      <div className="toolbar">
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        {error && <span className="err">{error}</span>}
      </div>
      <div className="mon-body">
        {st && !st.supported && (
          <div className="alerts"><div className="alert warning"><span className="lvl">n/a</span><span>Built-in replication needs Firebird 4.0 or newer; this server runs {st.engine_version}. The replication.conf helper below still works for planning.</span></div></div>
        )}
        {st?.supported && (
          <>
            <div className="cards">
              <div className="card"><div className="k">Replica mode</div><div className="v" style={{ fontSize: 16 }}>{st.replica_mode.replace("_", "-")}</div></div>
              <div className="card"><div className="k">Publication</div><div className="v" style={{ fontSize: 16 }}>{st.publication_enabled ? "enabled" : "disabled"}</div></div>
              <div className="card"><div className="k">Published tables</div><div className="v">{st.tables.filter((t: any) => t.published).length}<span className="muted" style={{ fontSize: 13 }}> / {st.tables.length}</span></div></div>
              <div className="card"><div className="k">Replication sequence</div><div className="v">{st.replication_sequence ?? "–"}</div></div>
              <div className="card"><div className="k">New tables</div><div className="v" style={{ fontSize: 16 }}>{st.auto_enable ? "auto-published" : "manual"}</div></div>
            </div>
            {st.warnings.length > 0 && <div className="alerts">{st.warnings.map((w: string, i: number) => <div key={i} className="alert warning"><span className="lvl">warning</span><span>{w}</span></div>)}</div>}
            <div className="muted" style={{ fontSize: 12 }}>Database GUID {st.guid} · {st.database_name}</div>

            <h3>Publication (primary side)</h3>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              {st.publication_enabled
                ? <button className="btn" onClick={() => act(() => repApi.publication(props.serverId, props.dbId, false))}>Disable publication</button>
                : <button className="btn primary" onClick={() => act(() => repApi.publication(props.serverId, props.dbId, true))}>Enable publication</button>}
              <span className="sep" style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />
              <button className="btn" disabled={!checked.size} onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "include", [...checked]))}>Include selected</button>
              <button className="btn" disabled={!checked.size} onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "exclude", [...checked]))}>Exclude selected</button>
              <button className="btn" onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "include", [], true))}>Include all (and future tables)</button>
              <button className="btn" onClick={() => act(() => repApi.tables(props.serverId, props.dbId, "exclude", [], true))}>Exclude all</button>
            </div>
            <div className="grid-wrap" style={{ marginTop: 8, maxHeight: 320 }}>
              <table className="grid">
                <thead><tr><th style={{ width: 30 }}><input type="checkbox" checked={checked.size > 0 && checked.size === st.tables.length} onChange={(e) => setChecked(e.target.checked ? new Set(st.tables.map((t: any) => t.name)) : new Set())} /></th><th>Table</th><th>Published</th><th>Primary/unique key</th></tr></thead>
                <tbody>
                  {st.tables.map((t: any) => (
                    <tr key={t.name}>
                      <td><input type="checkbox" checked={checked.has(t.name)} onChange={(e) => setChecked((c) => { const n = new Set(c); e.target.checked ? n.add(t.name) : n.delete(t.name); return n; })} /></td>
                      <td>{t.name}</td><td className={t.published ? "ok" : "muted"}>{t.published ? "✓ yes" : "no"}</td>
                      <td className={t.has_key ? "" : "warn"}>{t.has_key ? "yes" : "▲ no (uses DB_KEY)"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <h3>Replica mode</h3>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <div className="seg">
                {["none", "read_only", "read_write"].map((m) => (
                  <button key={m} className={st.replica_mode === m ? "on" : ""} disabled={st.replica_mode === m} onClick={() => confirmMode(m)}>{m.replace("_", "-")}</button>
                ))}
              </div>
              <span className="muted">{MODE_LABEL[st.replica_mode]}</span>
            </div>

            <h3>Status and lag</h3>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label className="muted">Compare with the other side:</label>
              <select className="text" value={peer} onChange={(e) => setPeer(e.target.value)}>
                <option value="">— choose the primary/replica database —</option>
                {allDbs.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
              </select>
            </div>
            {st.peer && (st.peer.error ? <div className="err">{st.peer.error}</div> : (
              <table className="kv" style={{ marginTop: 8 }}><tbody>
                <tr><td>Peer replica mode</td><td>{st.peer.replica_mode}</td></tr>
                <tr><td>Peer replication sequence</td><td>{st.peer.replication_sequence ?? "–"}</td></tr>
                <tr><td>Segments behind (approx.)</td><td className={st.peer.segments_behind > 1 ? "warn" : ""}>{st.peer.segments_behind ?? "–"}</td></tr>
                <tr><td>Peer GUID</td><td>{st.peer.guid}</td></tr>
              </tbody></table>
            ))}
            <p className="muted">Lag is approximated from RDB$GET_CONTEXT('SYSTEM', 'REPLICATION_SEQUENCE') on both sides (journal segment numbers). Exact apply status is only in the server's replication.log.</p>
            <button className="btn" onClick={() => repApi.log(props.serverId, props.dbId).then((r) => setLog(r.entries)).catch((e) => app.toast(errMsg(e), "error"))}>Show replication entries of firebird.log</button>
            {log && (log.length ? <div className="code-block" style={{ maxHeight: 260, overflow: "auto" }}>{log.join("\n\n")}</div> : <div className="muted">No replication-related entries in firebird.log.</div>)}
          </>
        )}
        <ConfHelper dbPath={st?.database_name} />
      </div>
    </div>
  );
}

const FIELDS: Record<string, [string, string, string][]> = {
  primary: [
    ["journal_directory", "Journal directory", "/var/lib/firebird/journal"],
    ["journal_archive_directory", "Journal archive directory", "/var/lib/firebird/archive"],
    ["journal_archive_timeout", "Archive timeout (s)", "60"],
    ["journal_segment_size", "Segment size", "16M"],
    ["journal_segment_count", "Segment count", "8"],
    ["journal_archive_command", "Archive command (optional)", "cp $(pathname) $(archivepathname)"],
    ["include_filter", "Include filter (regex)", ""],
    ["exclude_filter", "Exclude filter (regex)", ""],
  ],
  primary_sync: [["sync_replica", "Replica connection", "SYSDBA:password@replica-host:/data/replica.fdb"]],
  replica: [
    ["journal_source_directory", "Journal source directory", "/var/lib/firebird/archive"],
    ["source_guid", "Source GUID (optional)", "{...}"],
    ["apply_idle_timeout", "Apply idle timeout (s)", "10"],
    ["apply_error_timeout", "Apply error timeout (s)", "60"],
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
    const t = setTimeout(() => repApi.conf(body).then((r) => { setOut(r); setError(null); }).catch((e) => setError(errMsg(e))), 250);
    return () => clearTimeout(t);
  }, [body]);
  const set = (k: string, v: string) => setVals((x) => ({ ...x, [k]: v }));
  return (
    <>
      <h3>replication.conf helper</h3>
      <p className="muted" style={{ marginTop: 0 }}>Generates a <code>database = …</code> block for <code>replication.conf</code> in the Firebird root directory of the server (not readable remotely). The configuration is read when the database is opened by its first attachment.</p>
      <div className="split2">
        <div className="form-grid" style={{ gridTemplateColumns: "180px 1fr" }}>
          <label>Role</label>
          <select className="text" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="primary">Primary — asynchronous (journal files)</option>
            <option value="primary_sync">Primary — synchronous (direct to replica)</option>
            <option value="replica">Replica — applies archived journals</option>
          </select>
          <label>Database (path or alias)</label>
          <input className="text" value={vals.database ?? props.dbPath ?? ""} onChange={(e) => set("database", e.target.value)} />
          {FIELDS[role].map(([k, label, ph]) => (
            <Fragment key={k}><label>{label}</label><input className="text" placeholder={ph} value={vals[k] ?? ""} onChange={(e) => set(k, e.target.value)} /></Fragment>
          ))}
          <label>Logging</label>
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
                <button className="btn small" onClick={() => { navigator.clipboard?.writeText(out.text); app.toast("Copied"); }}><IconCopy size={12} /> Copy</button>
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
  app.openTab({ kind: "replication", title: `${name} · replication`, serverId: sid, dbId: dbid });
}

registerTabKind("replication", {
  icon: <span style={{ fontSize: 12 }}>⇄</span>,
  render: (t, visible) => <ReplicationTab serverId={t.serverId!} dbId={t.dbId!} visible={visible} />,
});

registerMenu((app, sel) => (sel.kind === "database" && sel.dbId
  ? [{ label: "Replication", onClick: () => openReplication(app, sel.serverId, sel.dbId!) }]
  : []));

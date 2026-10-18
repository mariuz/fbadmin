import { useEffect, useState } from "react";
import { api } from "../api";
import { errMsg, useApp } from "../context";
import { OBJECT_TYPES, typeInfo, type ObjectDetail, type Selection } from "../types";
import { SqlEditor } from "./SqlEditor";
import { ServerDialog } from "./dialogs";
import { getDashboardSections } from "../registry";
import { IconActivity, IconCopy, IconDatabase, IconServer, IconTerminal, TYPE_ICONS } from "./Icons";

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}

function Table(props: { rows: Array<Record<string, any>>; cols: Array<[string, string]> }) {
  if (!props.rows.length) return <div className="muted">None</div>;
  return (
    <div className="grid-wrap">
      <table className="grid">
        <thead><tr>{props.cols.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead>
        <tbody>
          {props.rows.map((r, i) => (
            <tr key={i}>{props.cols.map(([k]) => {
              const v = r[k];
              return <td key={k} className={v === null || v === undefined ? "null" : ""}>{Array.isArray(v) ? v.join(", ") : fmt(v)}</td>;
            })}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const yes = (b: unknown) => (b ? "✓" : "");

export function PropertiesPanel(props: { sel: Selection | null; detail: ObjectDetail | null; error: string | null }) {
  const { sel, detail } = props;
  if (!sel || (sel.kind !== "object" && sel.kind !== "column")) return <Hint />;
  if (props.error) return <div className="pad err">{props.error}</div>;
  if (!detail) return <div className="pad muted">Loading…</div>;
  const Icon = TYPE_ICONS[detail.type] ?? IconDatabase;
  if (sel.kind === "column") {
    const c = detail.columns?.find((x) => x.name === sel.column);
    if (!c) return <div className="pad muted">Column not found.</div>;
    return (
      <div className="pad">
        <h2>{sel.name}.{c.name}</h2>
        <table className="kv"><tbody>
          {[["Data type", c.data_type], ["Domain", c.domain], ["Not null", c.not_null], ["Default", c.default], ["Computed by", c.computed],
            ["Identity", c.identity], ["Primary key", c.primary_key], ["Character set", c.charset], ["Collation", c.collation], ["Comment", c.description]]
            .map(([k, v]) => <tr key={String(k)}><td>{String(k)}</td><td>{fmt(v)}</td></tr>)}
        </tbody></table>
      </div>
    );
  }
  return (
    <div className="pad">
      <h2><Icon size={18} /> {typeInfo(detail.type)?.singular} {detail.name}</h2>
      <table className="kv"><tbody>
        {Object.entries(detail.properties).map(([k, v]) => <tr key={k}><td>{k}</td><td style={{ whiteSpace: "pre-wrap" }}>{fmt(v)}</td></tr>)}
        {detail.returns && <tr><td>Returns</td><td className="mono">{detail.returns}</td></tr>}
      </tbody></table>
      {detail.columns && (
        <>
          <h3>Columns</h3>
          <Table rows={detail.columns.map((c) => ({ ...c, pk: yes(c.primary_key), nn: yes(c.not_null) }))}
            cols={[["position", "#"], ["name", "Name"], ["data_type", "Type"], ["domain", "Domain"], ["nn", "Not null"], ["pk", "PK"], ["default", "Default"], ["computed", "Computed"], ["identity", "Identity"], ["collation", "Collation"], ["description", "Comment"]]} />
        </>
      )}
      {detail.constraints && (
        <>
          <h3>Constraints</h3>
          <Table rows={detail.constraints.map((c) => ({ ...c, ref: c.ref_table ? `${c.ref_table} (${(c.ref_columns ?? []).join(", ")})` : c.source }))}
            cols={[["name", "Name"], ["type", "Type"], ["columns", "Columns"], ["ref", "References / check"], ["on_update", "On update"], ["on_delete", "On delete"], ["index", "Index"]]} />
        </>
      )}
      {detail.indices && (
        <>
          <h3>Indices</h3>
          <Table rows={detail.indices.map((i) => ({ ...i, u: yes(i.is_unique), d: yes(i.descending), a: i.inactive ? "inactive" : "active", cols: i.expr ?? i.columns.join(", ") }))}
            cols={[["name", "Name"], ["cols", "Columns / expression"], ["u", "Unique"], ["d", "Desc"], ["a", "State"], ["selectivity", "Selectivity"], ["constraint_name", "Constraint"]]} />
        </>
      )}
      {detail.triggers && (
        <>
          <h3>Triggers</h3>
          <Table rows={detail.triggers.map((t) => ({ ...t, a: t.inactive ? "inactive" : "active" }))}
            cols={[["name", "Name"], ["event", "Event"], ["position", "Position"], ["a", "State"]]} />
        </>
      )}
      {detail.parameters && (
        <>
          <h3>Parameters</h3>
          <Table rows={detail.parameters} cols={[["name", "Name"], ["direction", "Direction"], ["data_type", "Type"], ["description", "Comment"]]} />
        </>
      )}
    </div>
  );
}

export function SqlPanel(props: { sel: Selection | null; detail: ObjectDetail | null; error: string | null }) {
  const app = useApp();
  const { sel, detail } = props;
  if (!sel || (sel.kind !== "object" && sel.kind !== "column")) return <Hint text="Select an object to see its DDL." />;
  if (props.error) return <div className="pad err">{props.error}</div>;
  const ddl = detail?.ddl ?? "-- loading…";
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn" onClick={() => app.openQueryTool(sel.serverId, sel.dbId!, ddl)}><IconTerminal /> Open in Query Tool</button>
        <button className="btn" onClick={() => { navigator.clipboard?.writeText(ddl); app.toast("DDL copied", "info"); }}><IconCopy /> Copy</button>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}><SqlEditor value={ddl} readOnly /></div>
    </div>
  );
}

export function DependenciesPanel(props: { sel: Selection | null; detail: ObjectDetail | null }) {
  const app = useApp();
  const { sel, detail } = props;
  if (!sel || sel.kind !== "object") return <Hint text="Select an object to see what it depends on and what depends on it." />;
  if (!detail?.dependencies) return <div className="pad muted">{detail ? "Not available for this object type." : "Loading…"}</div>;
  const link = (r: Record<string, any>) => {
    const known = OBJECT_TYPES.some((t) => t.type === r.type);
    return known ? (
      <a href="#" onClick={(e) => { e.preventDefault(); app.select({ kind: "object", serverId: sel.serverId, dbId: sel.dbId, otype: r.type, name: r.name }); }}>{r.name}</a>
    ) : r.name;
  };
  const tbl = (rows: Array<Record<string, any>>) => rows.length ? (
    <div className="grid-wrap"><table className="grid"><thead><tr><th>Name</th><th>Type</th><th>Field</th></tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}><td>{link(r)}</td><td>{typeInfo(r.type)?.singular ?? r.type}</td><td>{r.field ?? ""}</td></tr>)}</tbody></table></div>
  ) : <div className="muted">None</div>;
  return (
    <div className="pad">
      <h3 style={{ marginTop: 0 }}>Depends on</h3>
      {tbl(detail.dependencies.depends_on)}
      <h3>Dependents (used by)</h3>
      {tbl(detail.dependencies.dependents)}
    </div>
  );
}

function Hint(props: { text?: string }) {
  return <div className="empty-state">{props.text ?? "Select an object in the browser to see its properties."}</div>;
}

export function Dashboard(props: { sel: Selection | null }) {
  const app = useApp();
  const { sel } = props;
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const srv = sel ? app.servers.find((s) => s.id === sel.serverId) : undefined;
  const scope = sel ? (sel.kind === "server" ? `s:${sel.serverId}` : `d:${sel.serverId}:${sel.dbId}`) : "";

  useEffect(() => {
    setData(null);
    setError(null);
    if (!sel) return;
    const p = sel.kind === "server" ? api.serverInfo(sel.serverId) : api.databaseInfo(sel.serverId, sel.dbId!);
    let stale = false;
    p.then((d) => !stale && setData({ ...d, __scope: scope })).catch((e) => !stale && setError(errMsg(e)));
    return () => { stale = true; };
  }, [scope]);

  if (!sel || !srv) {
    return (
      <div className="empty-state">
        <div className="big">Welcome to fbadmin</div>
        <div>A web administration tool for Firebird. Register a server to get started.</div>
        <button className="btn primary" onClick={() => app.showDialog(<ServerDialog />)}>Register server</button>
      </div>
    );
  }
  if (error) return <div className="pad err" style={{ whiteSpace: "pre-wrap" }}>{error}</div>;
  if (!data || data.__scope !== scope) return <div className="pad muted">Loading…</div>;

  if (sel.kind === "server") {
    return (
      <div className="pad">
        <h2><IconServer size={18} /> {srv.name}</h2>
        <div className="cards">
          <div className="card"><div className="k">Version</div><div className="v" style={{ fontSize: 15 }}>{data.server_version}</div></div>
          <div className="card"><div className="k">Attachments</div><div className="v">{data.attachment_count ?? "—"}</div></div>
          <div className="card"><div className="k">Attached databases</div><div className="v">{data.attached_databases?.length ?? 0}</div></div>
          <div className="card"><div className="k">Registered databases</div><div className="v">{srv.databases.length}</div></div>
        </div>
        <table className="kv"><tbody>
          <tr><td>Host</td><td>{srv.host}:{srv.port}</td></tr>
          <tr><td>User</td><td>{srv.user}{srv.role ? ` (role ${srv.role})` : ""}</td></tr>
          <tr><td>Architecture</td><td>{fmt(data.architecture)}</td></tr>
          <tr><td>Home directory</td><td>{fmt(data.home_directory)}</td></tr>
          <tr><td>Security database</td><td>{fmt(data.security_database)}</td></tr>
          <tr><td>Lock directory</td><td>{fmt(data.lock_directory)}</td></tr>
          <tr><td>Attached database files</td><td style={{ whiteSpace: "pre-wrap" }}>{(data.attached_databases ?? []).join("\n") || "—"}</td></tr>
        </tbody></table>
      </div>
    );
  }

  const info = data.info as Record<string, any>;
  const counts = data.counts as Record<string, number>;
  const gap = (info.next_transaction ?? 0) - (info.oldest_transaction ?? 0);
  const db = srv.databases.find((d) => d.id === sel.dbId);
  return (
    <div className="pad">
      <h2><IconDatabase size={18} /> {db?.name}
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={() => app.openQueryTool(sel.serverId, sel.dbId!)}><IconTerminal /> Query Tool</button>
        <button className="btn" onClick={() => app.openTab({ kind: "monitor", title: `${db?.name} · monitor`, serverId: sel.serverId, dbId: sel.dbId })}><IconActivity /> Monitor</button>
      </h2>
      <div className="cards">
        {OBJECT_TYPES.filter((t) => counts[t.type] !== undefined).map((t) => (
          <div key={t.type} className="card click" onClick={() => app.select({ kind: "folder", serverId: sel.serverId, dbId: sel.dbId, otype: t.type })}>
            <div className="k">{t.label}</div><div className="v">{counts[t.type]}</div>
          </div>
        ))}
      </div>
      <h3>Database</h3>
      <table className="kv"><tbody>
        <tr><td>File</td><td>{fmt(info.database_name)}</td></tr>
        <tr><td>Engine version</td><td>{fmt(info.engine_version)}</td></tr>
        <tr><td>ODS version</td><td>{info.ods_major}.{info.ods_minor}</td></tr>
        <tr><td>Page size</td><td>{fmt(info.page_size)}</td></tr>
        <tr><td>Pages / size</td><td>{fmt(info.pages)} pages · {((info.pages * info.page_size) / 1048576).toFixed(1)} MB</td></tr>
        <tr><td>Default charset</td><td>{fmt(info.charset)}</td></tr>
        <tr><td>SQL dialect</td><td>{fmt(info.sql_dialect)}</td></tr>
        <tr><td>Created</td><td>{fmt(info.creation_date)}</td></tr>
        <tr><td>Owner</td><td>{fmt(info.owner)}</td></tr>
        <tr><td>Forced writes</td><td>{fmt(info.forced_writes === 1)}</td></tr>
        <tr><td>Read only</td><td>{fmt(info.read_only === 1)}</td></tr>
        <tr><td>Sweep interval</td><td>{fmt(info.sweep_interval)}</td></tr>
        <tr><td>Page buffers</td><td>{fmt(info.page_buffers)}</td></tr>
        <tr><td>Shutdown mode</td><td>{["online", "multi-user", "single-user", "full"][info.shutdown_mode] ?? fmt(info.shutdown_mode)}</td></tr>
        {info.replica_mode !== undefined && <tr><td>Replica mode</td><td>{["none", "read-only", "read-write"][info.replica_mode] ?? info.replica_mode}</td></tr>}
        <tr><td>Connected as</td><td>{fmt(info.cur_user)} {info.cur_role && info.cur_role !== "NONE" ? `(${info.cur_role})` : ""} · {fmt(info.protocol)} · wire encrypted: {fmt(info.wire_encrypted)}</td></tr>
        <tr><td>Attachments</td><td>{fmt(info.attachments)}</td></tr>
      </tbody></table>
      <h3>Transactions</h3>
      <table className="kv"><tbody>
        <tr><td>Next transaction</td><td>{fmt(info.next_transaction)}</td></tr>
        <tr><td>Oldest interesting (OIT)</td><td>{fmt(info.oldest_transaction)}</td></tr>
        <tr><td>Oldest active (OAT)</td><td>{fmt(info.oldest_active)}</td></tr>
        <tr><td>Oldest snapshot (OST)</td><td>{fmt(info.oldest_snapshot)}</td></tr>
        <tr><td>Gap (next − OIT)</td><td className={gap > 50000 ? "warn" : ""}>{gap.toLocaleString()}{gap > 50000 ? " — consider a sweep" : ""}</td></tr>
      </tbody></table>
      {getDashboardSections().map((d) => <div key={d.id}>{d.render(sel)}</div>)}
    </div>
  );
}

export function FolderList(props: { sel: Selection }) {
  const app = useApp();
  const [items, setItems] = useState<Array<Record<string, any>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { sel } = props;
  useEffect(() => {
    setItems(null);
    api.objects(sel.serverId, sel.dbId!, sel.otype!).then(setItems).catch((e) => setError(errMsg(e)));
  }, [sel.serverId, sel.dbId, sel.otype, app.versions[`f:${sel.serverId}:${sel.dbId}:${sel.otype}`]]);
  const ti = typeInfo(sel.otype);
  if (error) return <div className="pad err">{error}</div>;
  if (!items) return <div className="pad muted">Loading…</div>;
  const cols = items.length ? Object.keys(items[0]).filter((k) => !["trigger_type", "kind"].includes(k)) : [];
  return (
    <div className="pad">
      <h2>{ti?.label} <span className="muted">({items.length})</span></h2>
      {items.length === 0 ? <div className="muted">None.</div> : (
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid">
            <thead><tr>{cols.map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr></thead>
            <tbody>{items.map((it) => (
              <tr key={it.name} style={{ cursor: "pointer" }} onClick={() => app.select({ kind: "object", serverId: sel.serverId, dbId: sel.dbId, otype: sel.otype, name: it.name })}>
                {cols.map((c) => <td key={c} className={it[c] === null ? "null" : ""}>{fmt(it[c])}</td>)}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}

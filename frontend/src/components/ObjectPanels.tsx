import { useEffect, useState } from "react";
import { api } from "../api";
import { errMsg, useApp } from "../context";
import { OBJECT_TYPES, typeInfo, type ObjectDetail, type Selection } from "../types";
import { SqlEditor } from "./SqlEditor";
import { ServerDialog } from "./dialogs";
import { getDashboardSections } from "../registry";
import { t } from "../i18n";
import { IconActivity, IconCopy, IconDatabase, IconServer, IconTerminal, TYPE_ICONS } from "./Icons";

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? t("Yes") : t("No");
  return String(v);
}

function Table(props: { rows: Array<Record<string, any>>; cols: Array<[string, string]> }) {
  if (!props.rows.length) return <div className="muted">{t("None")}</div>;
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
const typeLabel = (type: string) => {
  const ti = typeInfo(type);
  return ti ? t(ti.singular) : type;
};

export function PropertiesPanel(props: { sel: Selection | null; detail: ObjectDetail | null; error: string | null }) {
  const { sel, detail } = props;
  if (!sel || (sel.kind !== "object" && sel.kind !== "column")) return <Hint />;
  if (props.error) return <div className="pad err">{props.error}</div>;
  if (!detail) return <div className="pad muted">{t("Loading…")}</div>;
  const Icon = TYPE_ICONS[detail.type] ?? IconDatabase;
  if (sel.kind === "column") {
    const c = detail.columns?.find((x) => x.name === sel.column);
    if (!c) return <div className="pad muted">{t("Column not found.")}</div>;
    return (
      <div className="pad">
        <h2>{sel.name}.{c.name}</h2>
        <table className="kv"><tbody>
          {[[t("Data type"), c.data_type], [t("Domain"), c.domain], [t("Not null"), c.not_null], [t("Default"), c.default], [t("Computed by"), c.computed],
            [t("Identity"), c.identity], [t("Primary key"), c.primary_key], [t("Character set"), c.charset], [t("Collation"), c.collation], [t("Comment"), c.description]]
            .map(([k, v]) => <tr key={String(k)}><td>{String(k)}</td><td>{fmt(v)}</td></tr>)}
        </tbody></table>
      </div>
    );
  }
  return (
    <div className="pad">
      <h2><Icon size={18} /> {t(typeInfo(detail.type)?.singular ?? "")} {detail.name}</h2>
      <table className="kv"><tbody>
        {Object.entries(detail.properties).map(([k, v]) => <tr key={k}><td>{k}</td><td style={{ whiteSpace: "pre-wrap" }}>{fmt(v)}</td></tr>)}
        {detail.returns && <tr><td>{t("Returns")}</td><td className="mono">{detail.returns}</td></tr>}
      </tbody></table>
      {detail.columns && (
        <>
          <h3>{t("Columns")}</h3>
          <Table rows={detail.columns.map((c) => ({ ...c, pk: yes(c.primary_key), nn: yes(c.not_null) }))}
            cols={[["position", "#"], ["name", t("Name")], ["data_type", t("Type")], ["domain", t("Domain")], ["nn", t("Not null")], ["pk", t("PK")], ["default", t("Default")], ["computed", t("Computed")], ["identity", t("Identity")], ["collation", t("Collation")], ["description", t("Comment")]]} />
        </>
      )}
      {detail.constraints && (
        <>
          <h3>{t("Constraints")}</h3>
          <Table rows={detail.constraints.map((c) => ({ ...c, ref: c.ref_table ? `${c.ref_table} (${(c.ref_columns ?? []).join(", ")})` : c.source }))}
            cols={[["name", t("Name")], ["type", t("Type")], ["columns", t("Columns")], ["ref", t("References / check")], ["on_update", t("On update")], ["on_delete", t("On delete")], ["index", t("Index")]]} />
        </>
      )}
      {detail.indices && (
        <>
          <h3>{t("Indices")}</h3>
          <Table rows={detail.indices.map((i) => ({ ...i, u: yes(i.is_unique), d: yes(i.descending), a: i.inactive ? t("inactive") : t("active"), cols: i.expr ?? i.columns.join(", ") }))}
            cols={[["name", t("Name")], ["cols", t("Columns / expression")], ["u", t("Unique")], ["d", t("Desc")], ["a", t("State")], ["selectivity", t("Selectivity")], ["constraint_name", t("Constraint")]]} />
        </>
      )}
      {detail.triggers && (
        <>
          <h3>{t("Triggers")}</h3>
          <Table rows={detail.triggers.map((tr) => ({ ...tr, a: tr.inactive ? t("inactive") : t("active") }))}
            cols={[["name", t("Name")], ["event", t("Event")], ["position", t("Position")], ["a", t("State")]]} />
        </>
      )}
      {detail.parameters && (
        <>
          <h3>{t("Parameters")}</h3>
          <Table rows={detail.parameters} cols={[["name", t("Name")], ["direction", t("Direction")], ["data_type", t("Type")], ["description", t("Comment")]]} />
        </>
      )}
    </div>
  );
}

export function SqlPanel(props: { sel: Selection | null; detail: ObjectDetail | null; error: string | null }) {
  const app = useApp();
  const { sel, detail } = props;
  if (!sel || (sel.kind !== "object" && sel.kind !== "column")) return <Hint text={t("Select an object to see its DDL.")} />;
  if (props.error) return <div className="pad err">{props.error}</div>;
  const ddl = detail?.ddl ?? `-- ${t("loading…")}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn" onClick={() => app.openQueryTool(sel.serverId, sel.dbId!, ddl)}><IconTerminal /> {t("Open in Query Tool")}</button>
        <button className="btn" onClick={() => { navigator.clipboard?.writeText(ddl); app.toast(t("DDL copied"), "info"); }}><IconCopy /> {t("Copy")}</button>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}><SqlEditor value={ddl} readOnly /></div>
    </div>
  );
}

export function DependenciesPanel(props: { sel: Selection | null; detail: ObjectDetail | null }) {
  const app = useApp();
  const { sel, detail } = props;
  if (!sel || sel.kind !== "object") return <Hint text={t("Select an object to see what it depends on and what depends on it.")} />;
  if (!detail?.dependencies) return <div className="pad muted">{detail ? t("Not available for this object type.") : t("Loading…")}</div>;
  const link = (r: Record<string, any>) => {
    const known = OBJECT_TYPES.some((ot) => ot.type === r.type);
    return known ? (
      <a href="#" onClick={(e) => { e.preventDefault(); app.select({ kind: "object", serverId: sel.serverId, dbId: sel.dbId, otype: r.type, name: r.name }); }}>{r.name}</a>
    ) : r.name;
  };
  const tbl = (rows: Array<Record<string, any>>) => rows.length ? (
    <div className="grid-wrap"><table className="grid"><thead><tr><th>{t("Name")}</th><th>{t("Type")}</th><th>{t("Field")}</th></tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}><td>{link(r)}</td><td>{typeLabel(r.type)}</td><td>{r.field ?? ""}</td></tr>)}</tbody></table></div>
  ) : <div className="muted">{t("None")}</div>;
  return (
    <div className="pad">
      <h3 style={{ marginTop: 0 }}>{t("Depends on")}</h3>
      {tbl(detail.dependencies.depends_on)}
      <h3>{t("Dependents (used by)")}</h3>
      {tbl(detail.dependencies.dependents)}
    </div>
  );
}

function Hint(props: { text?: string }) {
  return <div className="empty-state">{props.text ?? t("Select an object in the browser to see its properties.")}</div>;
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
        <div className="big">{t("Welcome to fbadmin")}</div>
        <div>{t("A web administration tool for Firebird. Register a server to get started.")}</div>
        <button className="btn primary" onClick={() => app.showDialog(<ServerDialog />)}>{t("Register server")}</button>
      </div>
    );
  }
  if (error) return <div className="pad err" style={{ whiteSpace: "pre-wrap" }}>{error}</div>;
  // data may still belong to the previous selection on the first render after a change
  if (!data || data.__scope !== scope) return <div className="pad muted">{t("Loading…")}</div>;

  if (sel.kind === "server") {
    return (
      <div className="pad">
        <h2><IconServer size={18} /> {srv.name}</h2>
        <div className="cards">
          <div className="card"><div className="k">{t("Version")}</div><div className="v" style={{ fontSize: 15 }}>{data.server_version}</div></div>
          <div className="card"><div className="k">{t("Attachments")}</div><div className="v">{data.attachment_count ?? "—"}</div></div>
          <div className="card"><div className="k">{t("Attached databases")}</div><div className="v">{data.attached_databases?.length ?? 0}</div></div>
          <div className="card"><div className="k">{t("Registered databases")}</div><div className="v">{srv.databases.length}</div></div>
        </div>
        <table className="kv"><tbody>
          <tr><td>{t("Host")}</td><td>{srv.host}:{srv.port}</td></tr>
          <tr><td>{t("User")}</td><td>{srv.user}{srv.role ? " (" + t("role {role}", { role: srv.role }) + ")" : ""}</td></tr>
          <tr><td>{t("Architecture")}</td><td>{fmt(data.architecture)}</td></tr>
          <tr><td>{t("Home directory")}</td><td>{fmt(data.home_directory)}</td></tr>
          <tr><td>{t("Security database")}</td><td>{fmt(data.security_database)}</td></tr>
          <tr><td>{t("Lock directory")}</td><td>{fmt(data.lock_directory)}</td></tr>
          <tr><td>{t("Attached database files")}</td><td style={{ whiteSpace: "pre-wrap" }}>{(data.attached_databases ?? []).join("\n") || "—"}</td></tr>
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
        <button className="btn" onClick={() => app.openQueryTool(sel.serverId, sel.dbId!)}><IconTerminal /> {t("Query Tool")}</button>
        <button className="btn" onClick={() => app.openTab({ kind: "monitor", title: t("{name} · monitor", { name: String(db?.name) }), serverId: sel.serverId, dbId: sel.dbId })}><IconActivity /> {t("Monitor")}</button>
      </h2>
      <div className="cards">
        {OBJECT_TYPES.filter((ot) => counts[ot.type] !== undefined).map((ot) => (
          <div key={ot.type} className="card click" onClick={() => app.select({ kind: "folder", serverId: sel.serverId, dbId: sel.dbId, otype: ot.type })}>
            <div className="k">{t(ot.label)}</div><div className="v">{counts[ot.type]}</div>
          </div>
        ))}
      </div>
      <h3>{t("Database")}</h3>
      <table className="kv"><tbody>
        <tr><td>{t("File")}</td><td>{fmt(info.database_name)}</td></tr>
        <tr><td>{t("Engine version")}</td><td>{fmt(info.engine_version)}</td></tr>
        <tr><td>{t("ODS version")}</td><td>{info.ods_major}.{info.ods_minor}</td></tr>
        <tr><td>{t("Page size")}</td><td>{fmt(info.page_size)}</td></tr>
        <tr><td>{t("Pages / size")}</td><td>{t("{pages} pages · {size} MB", { pages: fmt(info.pages), size: ((info.pages * info.page_size) / 1048576).toFixed(1) })}</td></tr>
        <tr><td>{t("Default charset")}</td><td>{fmt(info.charset)}</td></tr>
        <tr><td>{t("SQL dialect")}</td><td>{fmt(info.sql_dialect)}</td></tr>
        <tr><td>{t("Created")}</td><td>{fmt(info.creation_date)}</td></tr>
        <tr><td>{t("Owner")}</td><td>{fmt(info.owner)}</td></tr>
        <tr><td>{t("Forced writes")}</td><td>{fmt(info.forced_writes === 1)}</td></tr>
        <tr><td>{t("Read only")}</td><td>{fmt(info.read_only === 1)}</td></tr>
        <tr><td>{t("Sweep interval")}</td><td>{fmt(info.sweep_interval)}</td></tr>
        <tr><td>{t("Page buffers")}</td><td>{fmt(info.page_buffers)}</td></tr>
        <tr><td>{t("Shutdown mode")}</td><td>{[t("online"), t("multi-user"), t("single-user"), t("full")][info.shutdown_mode] ?? fmt(info.shutdown_mode)}</td></tr>
        {info.replica_mode !== undefined && <tr><td>{t("Replica mode")}</td><td>{[t("none"), t("read-only"), t("read-write")][info.replica_mode] ?? info.replica_mode}</td></tr>}
        <tr><td>{t("Connected as")}</td><td>{fmt(info.cur_user)} {info.cur_role && info.cur_role !== "NONE" ? `(${info.cur_role})` : ""} · {fmt(info.protocol)} · {t("wire encrypted:")} {fmt(info.wire_encrypted)}</td></tr>
        <tr><td>{t("Attachments")}</td><td>{fmt(info.attachments)}</td></tr>
      </tbody></table>
      <h3>{t("Transactions")}</h3>
      <table className="kv"><tbody>
        <tr><td>{t("Next transaction")}</td><td>{fmt(info.next_transaction)}</td></tr>
        <tr><td>{t("Oldest interesting (OIT)")}</td><td>{fmt(info.oldest_transaction)}</td></tr>
        <tr><td>{t("Oldest active (OAT)")}</td><td>{fmt(info.oldest_active)}</td></tr>
        <tr><td>{t("Oldest snapshot (OST)")}</td><td>{fmt(info.oldest_snapshot)}</td></tr>
        <tr><td>{t("Gap (next − OIT)")}</td><td className={gap > 50000 ? "warn" : ""}>{gap.toLocaleString()}{gap > 50000 ? " — " + t("consider a sweep") : ""}</td></tr>
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
  if (!items) return <div className="pad muted">{t("Loading…")}</div>;
  const cols = items.length ? Object.keys(items[0]).filter((k) => !["trigger_type", "kind"].includes(k)) : [];
  return (
    <div className="pad">
      <h2>{ti ? t(ti.label) : ""} <span className="muted">({items.length})</span></h2>
      {items.length === 0 ? <div className="muted">{t("None.")}</div> : (
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

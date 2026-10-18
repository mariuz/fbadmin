import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { errMsg, keys, useApp } from "../context";
import type { DatabaseEntry, Server } from "../types";
import { Modal } from "./Modal";
import { SqlEditor } from "./SqlEditor";
import { IconPlus, IconTrash } from "./Icons";

function useSubmit() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

const quote = (n: string) => (/^[A-Z][A-Z0-9_$]*$/.test(n) ? n : `"${n.replace(/"/g, '""')}"`);
const str = (s: string) => `'${s.replace(/'/g, "''")}'`;

// ---------------------------------------------------------------- server
export function ServerDialog(props: { server?: Server }) {
  const app = useApp();
  const s = props.server;
  const [f, setF] = useState({
    name: s?.name ?? "",
    host: s?.host ?? "localhost",
    port: s?.port ?? 3050,
    user: s?.user ?? "SYSDBA",
    password: "",
    save_password: s?.save_password ?? true,
    role: s?.role ?? "",
    charset: s?.charset ?? "UTF8",
    wire_crypt: s?.wire_crypt ?? true,
    database: "",
  });
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  const submit = () =>
    run(async () => {
      const body: Record<string, unknown> = { ...f, name: f.name || f.host, role: f.role || null };
      delete body.database;
      if (s) {
        await api.updateServer(s.id, body);
        app.invalidate(keys.server(s.id));
      } else {
        const created = await api.addServer(body);
        if (f.database.trim()) {
          const path = f.database.trim();
          const name = path.split(/[\\/]/).pop()!.replace(/\.(fdb|gdb)$/i, "") || path;
          await api.addDatabase(created.id, { name, path });
        }
        if (f.password) await api.connectServer(created.id, f.password);
      }
      await app.reloadServers();
      app.toast(s ? "Server updated" : "Server registered");
      close();
    });
  return (
    <Modal
      title={s ? `Edit server – ${s.name}` : "Register server"}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={submit}>{s ? "Save" : "Register"}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">Name</label>
        <input className="text" autoFocus value={f.name} placeholder="e.g. Production" onChange={(e) => set("name", e.target.value)} />
        <label className="l">Host</label>
        <input className="text" value={f.host} onChange={(e) => set("host", e.target.value)} />
        <label className="l">Port</label>
        <input className="text" type="number" value={f.port} onChange={(e) => set("port", Number(e.target.value))} />
        <label className="l">User</label>
        <input className="text" value={f.user} onChange={(e) => set("user", e.target.value)} />
        <label className="l">Password</label>
        <input className="text" type="password" value={f.password} placeholder={s?.has_password ? "(unchanged)" : ""} onChange={(e) => set("password", e.target.value)} />
        <span />
        <label className="check"><input type="checkbox" checked={f.save_password} onChange={(e) => set("save_password", e.target.checked)} /> Save password</label>
        <label className="l">Role</label>
        <input className="text" value={f.role} onChange={(e) => set("role", e.target.value)} />
        <label className="l">Connection charset</label>
        <select className="text" value={f.charset} onChange={(e) => set("charset", e.target.value)}>
          {["UTF8", "NONE", "WIN1250", "WIN1251", "WIN1252", "ISO8859_1", "ISO8859_2"].map((c) => <option key={c}>{c}</option>)}
        </select>
        <span />
        <label className="check"><input type="checkbox" checked={f.wire_crypt} onChange={(e) => set("wire_crypt", e.target.checked)} /> Wire encryption</label>
        {!s && (
          <>
            <label className="l">First database</label>
            <input className="text" value={f.database} placeholder="optional: /path/to/db.fdb or alias" onChange={(e) => set("database", e.target.value)} />
          </>
        )}
      </div>
    </Modal>
  );
}

// -------------------------------------------------------------- database
export function DatabaseDialog(props: { server: Server; mode: "register" | "create" | "edit"; database?: DatabaseEntry }) {
  const app = useApp();
  const d = props.database;
  const [f, setF] = useState({ name: d?.name ?? "", path: d?.path ?? "", page_size: 16384, charset: "UTF8" });
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  const titles = { register: "Register database", create: "Create database", edit: "Edit database registration" };
  const submit = () =>
    run(async () => {
      const name = f.name || f.path.split(/[\\/]/).pop()!.replace(/\.(fdb|gdb)$/i, "") || f.path;
      if (props.mode === "create") await api.createDatabase(props.server.id, { ...f, name });
      else if (props.mode === "edit") await api.updateDatabase(props.server.id, d!.id, { name, path: f.path });
      else await api.addDatabase(props.server.id, { name, path: f.path });
      await app.reloadServers();
      app.invalidate(keys.server(props.server.id));
      app.toast(props.mode === "create" ? `Database ${name} created` : "Saved");
      close();
    });
  return (
    <Modal
      title={`${titles[props.mode]} on ${props.server.name}`}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.path} onClick={submit}>{props.mode === "create" ? "Create" : "Save"}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">Path or alias</label>
        <input className="text" autoFocus value={f.path} placeholder="/var/lib/firebird/data/mydb.fdb" onChange={(e) => set("path", e.target.value)} />
        <label className="l">Display name</label>
        <input className="text" value={f.name} placeholder="defaults to file name" onChange={(e) => set("name", e.target.value)} />
        {props.mode === "create" && (
          <>
            <label className="l">Page size</label>
            <select className="text" value={f.page_size} onChange={(e) => set("page_size", Number(e.target.value))}>
              {[4096, 8192, 16384, 32768].map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <label className="l">Default charset</label>
            <select className="text" value={f.charset} onChange={(e) => set("charset", e.target.value)}>
              {["UTF8", "NONE", "WIN1250", "WIN1251", "WIN1252", "ISO8859_1"].map((c) => <option key={c}>{c}</option>)}
            </select>
            <span />
            <span className="muted">The path is on the Firebird server's file system.</span>
          </>
        )}
      </div>
    </Modal>
  );
}

// -------------------------------------------------------------- password
export function PasswordDialog(props: { server: Server; resolve: (ok: boolean) => void }) {
  const app = useApp();
  const [pw, setPw] = useState("");
  const { busy, error, run } = useSubmit();
  const cancel = () => {
    props.resolve(false);
    app.showDialog(null);
  };
  const submit = () =>
    run(async () => {
      await api.connectServer(props.server.id, pw);
      props.resolve(true);
      app.showDialog(null);
    });
  return (
    <Modal
      title={`Connect to ${props.server.name}`}
      onClose={cancel}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={cancel}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={submit}>Connect</button>
        </>
      }
    >
      <div className="form">
        <label className="l">User</label>
        <span>{props.server.user}</span>
        <label className="l">Password</label>
        <input className="text" type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------- confirm
export function ConfirmDialog(props: {
  title: string;
  message: string;
  action: string;
  onConfirm: () => Promise<void>;
  confirmText?: string;
}) {
  const app = useApp();
  const [typed, setTyped] = useState("");
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  return (
    <Modal
      title={props.title}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>Cancel</button>
          <button
            className="btn danger solid"
            disabled={busy || (!!props.confirmText && typed !== props.confirmText)}
            onClick={() => run(async () => { await props.onConfirm(); close(); })}
          >
            {props.action}
          </button>
        </>
      }
    >
      <p style={{ whiteSpace: "pre-wrap", marginTop: 0 }}>{props.message}</p>
      {props.confirmText && (
        <>
          <p className="muted">Type <b>{props.confirmText}</b> to confirm.</p>
          <input className="text" autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: "100%" }} />
        </>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- backup
export function BackupDialog(props: { server: Server; dbId: string }) {
  const app = useApp();
  const db = props.server.databases.find((d) => d.id === props.dbId)!;
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
  const [f, setF] = useState({
    backup_file: db.path.replace(/\.(fdb|gdb)$/i, "") + `_${stamp}.fbk`,
    metadata_only: false,
    garbage_collect: true,
    transportable: true,
  });
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  return (
    <Modal
      title={`Backup ${db.name}`}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => {
            await api.backup(props.server.id, { database_id: props.dbId, ...f });
            app.jobStarted();
            app.toast("Backup started — see Processes", "info");
            close();
          })}>Start backup</button>
        </>
      }
    >
      <div className="form">
        <label className="l">Backup file</label>
        <input className="text" value={f.backup_file} onChange={(e) => setF({ ...f, backup_file: e.target.value })} />
        <span />
        <span className="muted">Path on the server. Uses the Services API (gbak -se).</span>
        <span />
        <label className="check"><input type="checkbox" checked={f.metadata_only} onChange={(e) => setF({ ...f, metadata_only: e.target.checked })} /> Metadata only</label>
        <span />
        <label className="check"><input type="checkbox" checked={f.garbage_collect} onChange={(e) => setF({ ...f, garbage_collect: e.target.checked })} /> Garbage collection</label>
        <span />
        <label className="check"><input type="checkbox" checked={f.transportable} onChange={(e) => setF({ ...f, transportable: e.target.checked })} /> Transportable format</label>
      </div>
    </Modal>
  );
}

// --------------------------------------------------------------- restore
export function RestoreDialog(props: { server: Server }) {
  const app = useApp();
  const [f, setF] = useState({ backup_file: "", database_path: "", replace: false, page_size: "", register_as: "", deactivate_indexes: false });
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  return (
    <Modal
      title={`Restore on ${props.server.name}`}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.backup_file || !f.database_path} onClick={() => run(async () => {
            await api.restore(props.server.id, {
              ...f,
              page_size: f.page_size ? Number(f.page_size) : null,
              register_as: f.register_as || null,
            });
            app.jobStarted();
            app.toast("Restore started — see Processes", "info");
            close();
          })}>Start restore</button>
        </>
      }
    >
      <div className="form">
        <label className="l">Backup file (.fbk)</label>
        <input className="text" autoFocus value={f.backup_file} onChange={(e) => setF({ ...f, backup_file: e.target.value })} />
        <label className="l">Target database</label>
        <input className="text" value={f.database_path} onChange={(e) => setF({ ...f, database_path: e.target.value })} />
        <label className="l">Page size</label>
        <select className="text" value={f.page_size} onChange={(e) => setF({ ...f, page_size: e.target.value })}>
          <option value="">(keep)</option>
          {[4096, 8192, 16384, 32768].map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <label className="l">Register as</label>
        <input className="text" value={f.register_as} placeholder="optional display name" onChange={(e) => setF({ ...f, register_as: e.target.value })} />
        <span />
        <label className="check"><input type="checkbox" checked={f.replace} onChange={(e) => setF({ ...f, replace: e.target.checked })} /> Replace existing database</label>
        <span />
        <label className="check"><input type="checkbox" checked={f.deactivate_indexes} onChange={(e) => setF({ ...f, deactivate_indexes: e.target.checked })} /> Deactivate indices</label>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------- create table
interface ColDef { name: string; type: string; size: string; notNull: boolean; pk: boolean; identity: boolean; dflt: string }
const TYPES = ["INTEGER", "BIGINT", "SMALLINT", "INT128", "VARCHAR", "CHAR", "NUMERIC", "DECIMAL", "DOUBLE PRECISION",
  "FLOAT", "DECFLOAT", "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "BLOB SUB_TYPE TEXT",
  "BLOB SUB_TYPE BINARY", "VARBINARY", "BINARY"];
const SIZED = new Set(["VARCHAR", "CHAR", "NUMERIC", "DECIMAL", "VARBINARY", "BINARY", "DECFLOAT"]);

export function CreateTableDialog(props: { serverId: string; dbId: string }) {
  const app = useApp();
  const [name, setName] = useState("NEW_TABLE");
  const [cols, setCols] = useState<ColDef[]>([
    { name: "ID", type: "BIGINT", size: "", notNull: true, pk: true, identity: true, dflt: "" },
    { name: "NAME", type: "VARCHAR", size: "100", notNull: true, pk: false, identity: false, dflt: "" },
  ]);
  const [tab, setTab] = useState<"cols" | "sql">("cols");
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  const upd = (i: number, k: keyof ColDef, v: unknown) => setCols((c) => c.map((x, j) => (j === i ? { ...x, [k]: v } : x)));

  const sql = useMemo(() => {
    const lines = cols.filter((c) => c.name.trim()).map((c) => {
      let s = `    ${quote(c.name.trim())} ${c.type}${SIZED.has(c.type) && c.size ? `(${c.size})` : ""}`;
      if (c.identity) s += " GENERATED BY DEFAULT AS IDENTITY";
      if (c.dflt) s += ` DEFAULT ${c.dflt}`;
      if (c.notNull) s += " NOT NULL";
      return s;
    });
    const pk = cols.filter((c) => c.pk && c.name.trim()).map((c) => quote(c.name.trim()));
    if (pk.length) lines.push(`    CONSTRAINT ${quote("PK_" + name.trim().replace(/"/g, ""))} PRIMARY KEY (${pk.join(", ")})`);
    return `CREATE TABLE ${quote(name.trim())} (\n${lines.join(",\n")}\n);\n`;
  }, [name, cols]);

  return (
    <Modal
      wide
      title="Create table"
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={() => { app.openQueryTool(props.serverId, props.dbId, sql); close(); }}>Open in Query Tool</button>
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => {
            const r = await api.execute(props.serverId, props.dbId, sql);
            if (r.error) throw new Error(r.error.message);
            app.invalidate(keys.folder(props.serverId, props.dbId, "tables"));
            app.invalidate(keys.db(props.serverId, props.dbId));
            app.toast(`Table ${name} created`);
            close();
          })}>Create</button>
        </>
      }
    >
      <div className="form" style={{ marginBottom: 12 }}>
        <label className="l">Table name</label>
        <input className="text" value={name} onChange={(e) => setName(e.target.value.toUpperCase())} />
      </div>
      <div className="subtabs">
        <button className={tab === "cols" ? "active" : ""} onClick={() => setTab("cols")}>Columns</button>
        <button className={tab === "sql" ? "active" : ""} onClick={() => setTab("sql")}>SQL</button>
      </div>
      {tab === "cols" ? (
        <table className="coltable" style={{ width: "100%" }}>
          <thead>
            <tr className="muted"><td>Name</td><td>Type</td><td style={{ width: 80 }}>Size</td><td>Default</td><td>Not null</td><td>PK</td><td>Identity</td><td /></tr>
          </thead>
          <tbody>
            {cols.map((c, i) => (
              <tr key={i}>
                <td><input className="text" value={c.name} onChange={(e) => upd(i, "name", e.target.value.toUpperCase())} /></td>
                <td>
                  <select className="text" value={c.type} onChange={(e) => upd(i, "type", e.target.value)}>
                    {TYPES.map((t) => <option key={t}>{t}</option>)}
                  </select>
                </td>
                <td><input className="text" value={c.size} disabled={!SIZED.has(c.type)} placeholder={c.type === "NUMERIC" || c.type === "DECIMAL" ? "18,2" : ""} onChange={(e) => upd(i, "size", e.target.value)} /></td>
                <td><input className="text" value={c.dflt} onChange={(e) => upd(i, "dflt", e.target.value)} /></td>
                <td style={{ textAlign: "center" }}><input type="checkbox" checked={c.notNull} onChange={(e) => upd(i, "notNull", e.target.checked)} /></td>
                <td style={{ textAlign: "center" }}><input type="checkbox" checked={c.pk} onChange={(e) => upd(i, "pk", e.target.checked)} /></td>
                <td style={{ textAlign: "center" }}><input type="checkbox" checked={c.identity} onChange={(e) => upd(i, "identity", e.target.checked)} /></td>
                <td><button className="btn icon small" title="Remove column" onClick={() => setCols(cols.filter((_, j) => j !== i))}><IconTrash size={13} /></button></td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr><td colSpan={8}><button className="btn small" onClick={() => setCols([...cols, { name: "", type: "VARCHAR", size: "50", notNull: false, pk: false, identity: false, dflt: "" }])}><IconPlus size={13} /> Add column</button></td></tr>
          </tfoot>
        </table>
      ) : (
        <div style={{ height: 280, border: "1px solid var(--border)", borderRadius: 6 }}>
          <SqlEditor value={sql} readOnly />
        </div>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------------ user
export function UserDialog(props: { serverId: string; dbId: string; name?: string }) {
  const app = useApp();
  const editing = !!props.name;
  const [f, setF] = useState({ name: props.name ?? "", password: "", first: "", middle: "", last: "", active: true, admin: false });
  const [tab, setTab] = useState<"general" | "sql">("general");
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  useEffect(() => {
    if (!props.name) return;
    api.object(props.serverId, props.dbId, "users", props.name).then((o) => {
      const p = o.properties as Record<string, any>;
      setF((x) => ({ ...x, first: p["First name"] ?? "", middle: p["Middle name"] ?? "", last: p["Last name"] ?? "", active: !!p["Active"], admin: !!p["Admin"] }));
    });
  }, []);
  const sql = useMemo(() => {
    let s = `${editing ? "ALTER" : "CREATE"} USER ${quote(f.name)}`;
    if (f.password) s += ` PASSWORD ${str(f.password)}`;
    s += ` FIRSTNAME ${str(f.first)} MIDDLENAME ${str(f.middle)} LASTNAME ${str(f.last)}`;
    s += f.active ? " ACTIVE" : " INACTIVE";
    s += f.admin ? " GRANT ADMIN ROLE" : editing ? " REVOKE ADMIN ROLE" : "";
    return s + ";\n";
  }, [f, editing]);
  const shownSql = f.password ? sql.replace(str(f.password), "'********'") : sql;
  return (
    <Modal
      title={editing ? `Edit user ${props.name}` : "Create user"}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.name || (!editing && !f.password)} onClick={() => run(async () => {
            const r = await api.execute(props.serverId, props.dbId, sql);
            if (r.error) throw new Error(r.error.message);
            app.invalidate(keys.folder(props.serverId, props.dbId, "users"));
            app.toast(editing ? "User updated" : `User ${f.name} created`);
            close();
          })}>{editing ? "Save" : "Create"}</button>
        </>
      }
    >
      <div className="subtabs">
        <button className={tab === "general" ? "active" : ""} onClick={() => setTab("general")}>General</button>
        <button className={tab === "sql" ? "active" : ""} onClick={() => setTab("sql")}>SQL</button>
      </div>
      {tab === "general" ? (
        <div className="form">
          <label className="l">User name</label>
          <input className="text" value={f.name} disabled={editing} autoFocus onChange={(e) => setF({ ...f, name: e.target.value.toUpperCase() })} />
          <label className="l">Password</label>
          <input className="text" type="password" value={f.password} placeholder={editing ? "(unchanged)" : ""} onChange={(e) => setF({ ...f, password: e.target.value })} />
          <label className="l">First name</label>
          <input className="text" value={f.first} onChange={(e) => setF({ ...f, first: e.target.value })} />
          <label className="l">Middle name</label>
          <input className="text" value={f.middle} onChange={(e) => setF({ ...f, middle: e.target.value })} />
          <label className="l">Last name</label>
          <input className="text" value={f.last} onChange={(e) => setF({ ...f, last: e.target.value })} />
          <span />
          <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => setF({ ...f, active: e.target.checked })} /> Active</label>
          <span />
          <label className="check"><input type="checkbox" checked={f.admin} onChange={(e) => setF({ ...f, admin: e.target.checked })} /> Admin (RDB$ADMIN in security database)</label>
        </div>
      ) : (
        <pre className="log">{shownSql}</pre>
      )}
    </Modal>
  );
}

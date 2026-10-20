import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { errMsg, keys, useApp } from "../context";
import type { DatabaseEntry, Server } from "../types";
import { Modal } from "./Modal";
import { canShareServers } from "../features/security/state";
import { t } from "../i18n";

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
    wire_compress: s?.wire_compress ?? false,
    auth_plugin: s?.auth_plugin ?? "",
    read_only: s?.read_only ?? false,
    shared: s?.shared ?? false,
    database: "",
  });
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const { busy, error, run } = useSubmit();
  const close = () => app.showDialog(null);
  const submit = () =>
    run(async () => {
      const { database: _firstDb, ...fields } = f;
      const body = { ...fields, name: f.name || f.host, role: f.role || null, auth_plugin: f.auth_plugin || null };
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
      app.toast(s ? t("Server updated") : t("Server registered"));
      close();
    });
  return (
    <Modal
      title={s ? t("Edit server – {name}", { name: s.name }) : t("Register server")}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={submit}>{s ? t("Save") : t("Register")}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">{t("Name")}</label>
        <input className="text" autoFocus value={f.name} placeholder={t("e.g. Production")} onChange={(e) => set("name", e.target.value)} />
        <label className="l">{t("Host")}</label>
        <input className="text" value={f.host} onChange={(e) => set("host", e.target.value)} />
        <label className="l">{t("Port")}</label>
        <input className="text" type="number" value={f.port} onChange={(e) => set("port", Number(e.target.value))} />
        <label className="l">{t("User")}</label>
        <input className="text" value={f.user} onChange={(e) => set("user", e.target.value)} />
        <label className="l">{t("Password")}</label>
        <input className="text" type="password" value={f.password} placeholder={s?.has_password ? t("(unchanged)") : ""} onChange={(e) => set("password", e.target.value)} />
        <span />
        <label className="check"><input type="checkbox" checked={f.save_password} onChange={(e) => set("save_password", e.target.checked)} /> {t("Save password")}</label>
        <label className="l">{t("Role")}</label>
        <input className="text" value={f.role} onChange={(e) => set("role", e.target.value)} />
        <label className="l">{t("Connection charset")}</label>
        <select className="text" value={f.charset} onChange={(e) => set("charset", e.target.value)}>
          {["UTF8", "NONE", "WIN1250", "WIN1251", "WIN1252", "ISO8859_1", "ISO8859_2"].map((c) => <option key={c}>{c}</option>)}
        </select>
        <span />
        <label className="check"><input type="checkbox" checked={f.wire_crypt} onChange={(e) => set("wire_crypt", e.target.checked)} /> {t("Wire encryption")}</label>
        <span />
        <label className="check" title={t("zlib compression of the wire protocol (Firebird 3+); helps on slow links")}>
          <input type="checkbox" checked={f.wire_compress} onChange={(e) => set("wire_compress", e.target.checked)} /> {t("Wire compression")}</label>
        <label className="l">{t("Authentication plugin")}</label>
        <select className="text" value={f.auth_plugin} onChange={(e) => set("auth_plugin", e.target.value)}>
          <option value="">{t("default (Srp256)")}</option>
          {["Srp256", "Srp", "Legacy_Auth"].map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <span />
        <label className="check" title={t("Production safety: only queries are allowed; data edits, DDL and other changes are refused")}>
          <input type="checkbox" checked={f.read_only} onChange={(e) => set("read_only", e.target.checked)} /> {t("Read-only (block everything but SELECT)")}</label>
        {(canShareServers() || s?.shared) && <>
          <span />
          <label className="check" title={t("Visible to every fbadmin user; each user enters their own Firebird password")}>
            <input type="checkbox" checked={f.shared} disabled={!canShareServers()} onChange={(e) => set("shared", e.target.checked)} /> {t("Shared with all fbadmin users")}</label>
        </>}
        {!s && (
          <>
            <label className="l">{t("First database")}</label>
            <input className="text" value={f.database} placeholder={t("optional: /path/to/db.fdb or alias")} onChange={(e) => set("database", e.target.value)} />
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
  const titles = { register: t("Register database"), create: t("Create database"), edit: t("Edit database registration") };
  const submit = () =>
    run(async () => {
      const name = f.name || f.path.split(/[\\/]/).pop()!.replace(/\.(fdb|gdb)$/i, "") || f.path;
      if (props.mode === "create") await api.createDatabase(props.server.id, { ...f, name });
      else if (props.mode === "edit") await api.updateDatabase(props.server.id, d!.id, { name, path: f.path });
      else await api.addDatabase(props.server.id, { name, path: f.path });
      await app.reloadServers();
      app.invalidate(keys.server(props.server.id));
      app.toast(props.mode === "create" ? t("Database {name} created", { name }) : t("Saved"));
      close();
    });
  return (
    <Modal
      title={t("{title} on {server}", { title: titles[props.mode], server: props.server.name })}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || !f.path} onClick={submit}>{props.mode === "create" ? t("Create") : t("Save")}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">{t("Path or alias")}</label>
        <input className="text" autoFocus value={f.path} placeholder="/var/lib/firebird/data/mydb.fdb" onChange={(e) => set("path", e.target.value)} />
        <label className="l">{t("Display name")}</label>
        <input className="text" value={f.name} placeholder={t("defaults to file name")} onChange={(e) => set("name", e.target.value)} />
        {props.mode === "create" && (
          <>
            <label className="l">{t("Page size")}</label>
            <select className="text" value={f.page_size} onChange={(e) => set("page_size", Number(e.target.value))}>
              {[4096, 8192, 16384, 32768].map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <label className="l">{t("Default charset")}</label>
            <select className="text" value={f.charset} onChange={(e) => set("charset", e.target.value)}>
              {["UTF8", "NONE", "WIN1250", "WIN1251", "WIN1252", "ISO8859_1"].map((c) => <option key={c}>{c}</option>)}
            </select>
            <span />
            <span className="muted">{t("The path is on the Firebird server's file system.")}</span>
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
      title={t("Connect to {name}", { name: props.server.name })}
      onClose={cancel}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={cancel}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={submit}>{t("Connect")}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">{t("User")}</label>
        <span>{props.server.user}</span>
        <label className="l">{t("Password")}</label>
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
  const [typePre, typePost = ""] = t("Type {text} to confirm.").split("{text}");
  return (
    <Modal
      title={props.title}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
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
          <p className="muted">{typePre}<b>{props.confirmText}</b>{typePost}</p>
          <input className="text" autoFocus aria-label={t("Type {text} to confirm.", { text: props.confirmText })} value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: "100%" }} />
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
      title={t("Backup {name}", { name: db.name })}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => {
            await api.backup(props.server.id, { database_id: props.dbId, ...f });
            app.jobStarted();
            app.toast(t("Backup started — see Processes"), "info");
            close();
          })}>{t("Start backup")}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">{t("Backup file")}</label>
        <input className="text" value={f.backup_file} onChange={(e) => setF({ ...f, backup_file: e.target.value })} />
        <span />
        <span className="muted">{t("Path on the server. Uses the Services API (gbak -se).")}</span>
        <span />
        <label className="check"><input type="checkbox" checked={f.metadata_only} onChange={(e) => setF({ ...f, metadata_only: e.target.checked })} /> {t("Metadata only")}</label>
        <span />
        <label className="check"><input type="checkbox" checked={f.garbage_collect} onChange={(e) => setF({ ...f, garbage_collect: e.target.checked })} /> {t("Garbage collection")}</label>
        <span />
        <label className="check"><input type="checkbox" checked={f.transportable} onChange={(e) => setF({ ...f, transportable: e.target.checked })} /> {t("Transportable format")}</label>
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
      title={t("Restore on {name}", { name: props.server.name })}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || !f.backup_file || !f.database_path} onClick={() => run(async () => {
            await api.restore(props.server.id, {
              ...f,
              page_size: f.page_size ? Number(f.page_size) : null,
              register_as: f.register_as || null,
            });
            app.jobStarted();
            app.toast(t("Restore started — see Processes"), "info");
            close();
          })}>{t("Start restore")}</button>
        </>
      }
    >
      <div className="form">
        <label className="l">{t("Backup file (.fbk)")}</label>
        <input className="text" autoFocus value={f.backup_file} onChange={(e) => setF({ ...f, backup_file: e.target.value })} />
        <label className="l">{t("Target database")}</label>
        <input className="text" value={f.database_path} onChange={(e) => setF({ ...f, database_path: e.target.value })} />
        <label className="l">{t("Page size")}</label>
        <select className="text" value={f.page_size} onChange={(e) => setF({ ...f, page_size: e.target.value })}>
          <option value="">{t("(keep)")}</option>
          {[4096, 8192, 16384, 32768].map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <label className="l">{t("Register as")}</label>
        <input className="text" value={f.register_as} placeholder={t("optional display name")} onChange={(e) => setF({ ...f, register_as: e.target.value })} />
        <span />
        <label className="check"><input type="checkbox" checked={f.replace} onChange={(e) => setF({ ...f, replace: e.target.checked })} /> {t("Replace existing database")}</label>
        <span />
        <label className="check"><input type="checkbox" checked={f.deactivate_indexes} onChange={(e) => setF({ ...f, deactivate_indexes: e.target.checked })} /> {t("Deactivate indices")}</label>
      </div>
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
      title={editing ? t("Edit user {name}", { name: props.name ?? "" }) : t("Create user")}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || !f.name || (!editing && !f.password)} onClick={() => run(async () => {
            const r = await api.execute(props.serverId, props.dbId, sql);
            if (r.error) throw new Error(r.error.message);
            app.invalidate(keys.folder(props.serverId, props.dbId, "users"));
            app.toast(editing ? t("User updated") : t("User {name} created", { name: f.name }));
            close();
          })}>{editing ? t("Save") : t("Create")}</button>
        </>
      }
    >
      <div className="subtabs">
        <button className={tab === "general" ? "active" : ""} onClick={() => setTab("general")}>{t("General")}</button>
        <button className={tab === "sql" ? "active" : ""} onClick={() => setTab("sql")}>{t("SQL")}</button>
      </div>
      {tab === "general" ? (
        <div className="form">
          <label className="l">{t("User name")}</label>
          <input className="text" value={f.name} disabled={editing} autoFocus onChange={(e) => setF({ ...f, name: e.target.value.toUpperCase() })} />
          <label className="l">{t("Password")}</label>
          <input className="text" type="password" value={f.password} placeholder={editing ? t("(unchanged)") : ""} onChange={(e) => setF({ ...f, password: e.target.value })} />
          <label className="l">{t("First name")}</label>
          <input className="text" value={f.first} onChange={(e) => setF({ ...f, first: e.target.value })} />
          <label className="l">{t("Middle name")}</label>
          <input className="text" value={f.middle} onChange={(e) => setF({ ...f, middle: e.target.value })} />
          <label className="l">{t("Last name")}</label>
          <input className="text" value={f.last} onChange={(e) => setF({ ...f, last: e.target.value })} />
          <span />
          <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => setF({ ...f, active: e.target.checked })} /> {t("Active")}</label>
          <span />
          <label className="check"><input type="checkbox" checked={f.admin} onChange={(e) => setF({ ...f, admin: e.target.checked })} /> {t("Admin (RDB$ADMIN in security database)")}</label>
        </div>
      ) : (
        <pre className="log">{shownSql}</pre>
      )}
    </Modal>
  );
}

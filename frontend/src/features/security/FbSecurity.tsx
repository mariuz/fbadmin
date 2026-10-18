/** Firebird security of one database (SQL) and of one server (Services API). */
import { useEffect, useState, type ReactNode } from "react";
import { errMsg, useApp } from "../../context";
import { Modal } from "../../components/Modal";
import { IconPlus, IconRefresh, IconTrash } from "../../components/Icons";
import { secApi, type FbUser, type Mapping, type RoleInfo, type SqlResult } from "./api";

type Props = { serverId: string; dbId: string };

/** Dialog that shows the generated SQL (preview) before running it. */
function SqlDialog(props: {
  title: string;
  children: ReactNode;
  run: (preview: boolean) => Promise<SqlResult | unknown>;
  onDone: () => void;
  wide?: boolean;
  okLabel?: string;
}) {
  const app = useApp();
  const [sql, setSql] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const close = () => app.showDialog(null);
  const go = async (preview: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const r = (await props.run(preview)) as SqlResult | undefined;
      if (preview) setSql(r?.sql ?? []);
      else {
        app.toast(props.okLabel ? `${props.okLabel}: done` : "Done");
        props.onDone();
        close();
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={props.title} onClose={close} wide={props.wide} footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>Cancel</button>
      <button className="btn" disabled={busy} onClick={() => go(true)}>Preview SQL</button>
      <button className="btn primary" disabled={busy} onClick={() => go(false)}>{props.okLabel ?? "Save"}</button>
    </>}>
      {props.children}
      {sql && <pre className="log sec-sql">{sql.map((s) => s + ";").join("\n")}</pre>}
    </Modal>
  );
}

export function FbSecurityPanel(props: Props) {
  const [tab, setTab] = useState<"users" | "roles" | "mappings">("users");
  return (
    <div className="pad sec-panel">
      <div className="subtabs">
        <button className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>Users</button>
        <button className={tab === "roles" ? "active" : ""} onClick={() => setTab("roles")}>Roles &amp; system privileges</button>
        <button className={tab === "mappings" ? "active" : ""} onClick={() => setTab("mappings")}>Mappings</button>
      </div>
      {tab === "users" && <UsersTab {...props} />}
      {tab === "roles" && <RolesTab {...props} />}
      {tab === "mappings" && <MappingsTab {...props} />}
    </div>
  );
}

// ---------------------------------------------------------------- users (SQL)
function UsersTab(props: Props) {
  const app = useApp();
  const [data, setData] = useState<Awaited<ReturnType<typeof secApi.fbUsers>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => secApi.fbUsers(props.serverId, props.dbId).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  const edit = (u?: FbUser) => app.showDialog(<FbUserDialog {...props} user={u} plugins={data?.plugins ?? ["Srp"]} onDone={load} />);
  const drop = async (u: FbUser) => {
    if (!confirm(`Drop user ${u.name}${u.plugin ? ` (${u.plugin})` : ""}?`)) return;
    try { await secApi.dropFbUser(props.serverId, props.dbId, u.name, u.plugin); app.toast(`User ${u.name} dropped`); load(); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <>
      <div className="sec-bar">
        <button className="btn primary" onClick={() => edit()}><IconPlus /> Create user</button>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        {data && <span className="muted">Security database: <b>{data.security_database ?? "?"}</b> · plugins: {data.plugins.join(", ")} · signed in as {data.current_user}</span>}
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid">
            <thead><tr><th>User</th><th>Plugin</th><th>Name</th><th>Active</th><th title="GRANT ADMIN ROLE in the security database">Admin role</th><th title="RDB$ADMIN granted in this database">RDB$ADMIN</th><th>Tags</th><th>Description</th><th /></tr></thead>
            <tbody>
              {data.users.map((u) => (
                <tr key={u.name + "/" + u.plugin}>
                  <td><b>{u.name}</b></td>
                  <td>{u.plugin}</td>
                  <td>{[u.first_name, u.middle_name, u.last_name].filter(Boolean).join(" ")}</td>
                  <td>{u.active ? "yes" : <span className="err">no</span>}</td>
                  <td>{u.admin ? "✓" : ""}</td>
                  <td>{u.db_admin ? "✓" : ""}</td>
                  <td>{Object.entries(u.tags).map(([k, v]) => <span key={k} className="pill tag">{k}={v}</span>)}</td>
                  <td>{u.description}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn small" onClick={() => edit(u)}>Edit</button>{" "}
                    <button className="btn small danger" onClick={() => drop(u)}><IconTrash size={12} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function FbUserDialog(props: Props & { user?: FbUser; plugins: string[]; onDone: () => void }) {
  const u = props.user;
  const [f, setF] = useState({
    name: u?.name ?? "", password: "", first_name: u?.first_name ?? "", middle_name: u?.middle_name ?? "", last_name: u?.last_name ?? "",
    active: u?.active ?? true, admin: u?.admin ?? false, db_admin: u?.db_admin ?? false, plugin: u?.plugin ?? "",
    description: u?.description ?? "",
  });
  const [tags, setTags] = useState<Array<[string, string]>>(Object.entries(u?.tags ?? {}));
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const body = () => {
    const tagObj = Object.fromEntries(tags.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v]));
    const removed = Object.keys(u?.tags ?? {}).filter((k) => !(k in tagObj));
    const changed = <T,>(now: T, before: T | undefined) => (u && now === before ? null : now);
    return {
      ...f, plugin: f.plugin || null, password: f.password || null, tags: tagObj, drop_tags: removed,
      admin: changed(f.admin, u?.admin), db_admin: changed(f.db_admin, u?.db_admin),
      description: u ? (f.description !== (u.description ?? "") ? f.description : null) : f.description || null,
    };
  };
  const run = (preview: boolean) => u
    ? secApi.alterFbUser(props.serverId, props.dbId, u.name, body(), preview)
    : secApi.createFbUser(props.serverId, props.dbId, body(), preview);
  return (
    <SqlDialog title={u ? `Alter user ${u.name}` : "Create user"} run={run} onDone={props.onDone} okLabel={u ? "Alter user" : "Create user"}>
      <div className="form">
        <label className="l">User name</label>
        <input className="text" autoFocus={!u} disabled={!!u} value={f.name} placeholder='JOHN, or "MixedCase"' onChange={(e) => set("name", e.target.value)} />
        <label className="l">{u ? "New password" : "Password"}</label>
        <input className="text" type="password" autoComplete="new-password" placeholder={u ? "(unchanged)" : ""} value={f.password} onChange={(e) => set("password", e.target.value)} />
        <label className="l">First / middle / last name</label>
        <div style={{ display: "flex", gap: 6 }}>
          <input className="text" style={{ flex: 1, minWidth: 0 }} value={f.first_name} onChange={(e) => set("first_name", e.target.value)} />
          <input className="text" style={{ flex: 1, minWidth: 0 }} value={f.middle_name} onChange={(e) => set("middle_name", e.target.value)} />
          <input className="text" style={{ flex: 1, minWidth: 0 }} value={f.last_name} onChange={(e) => set("last_name", e.target.value)} />
        </div>
        <label className="l">Plugin (USING PLUGIN)</label>
        <div>
          <input className="text" list="fb-user-plugins" value={f.plugin} disabled={!!u} placeholder="default (first UserManager plugin)" onChange={(e) => set("plugin", e.target.value)} />
          <datalist id="fb-user-plugins">{[...new Set([...props.plugins, "Srp", "Legacy_UserManager"])].map((p) => <option key={p} value={p} />)}</datalist>
        </div>
        <span />
        <div className="sec-checks">
          <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => set("active", e.target.checked)} /> Active</label>
          <label className="check" title="Administrator of the security database (can manage users)"><input type="checkbox" checked={f.admin} onChange={(e) => set("admin", e.target.checked)} /> GRANT ADMIN ROLE</label>
          <label className="check" title="GRANT RDB$ADMIN TO USER in this database"><input type="checkbox" checked={f.db_admin} onChange={(e) => set("db_admin", e.target.checked)} /> RDB$ADMIN in this database</label>
        </div>
        <label className="l">Tags</label>
        <div className="sec-tags">
          {tags.map(([k, v], i) => (
            <div key={i} className="sec-tag-row">
              <input className="text" placeholder="key" value={k} onChange={(e) => setTags(tags.map((t, j) => (j === i ? [e.target.value, t[1]] : t)))} />
              <span>=</span>
              <input className="text" placeholder="value" value={v} onChange={(e) => setTags(tags.map((t, j) => (j === i ? [t[0], e.target.value] : t)))} />
              <button className="btn small" onClick={() => setTags(tags.filter((_, j) => j !== i))}><IconTrash size={12} /></button>
            </div>
          ))}
          <button className="btn small" onClick={() => setTags([...tags, ["", ""]])}><IconPlus size={12} /> Add tag</button>
        </div>
        <label className="l">Description</label>
        <input className="text" value={f.description} onChange={(e) => set("description", e.target.value)} />
      </div>
    </SqlDialog>
  );
}

// ---------------------------------------------------------------- roles / system privileges
function RolesTab(props: Props) {
  const app = useApp();
  const [data, setData] = useState<Awaited<ReturnType<typeof secApi.roles>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => secApi.roles(props.serverId, props.dbId).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  const edit = (r?: RoleInfo) => data && app.showDialog(<RoleDialog {...props} role={r} all={data.privileges} supported={data.supported} onDone={load} />);
  const grant = (r: RoleInfo) => app.showDialog(<GrantDialog {...props} role={r} onDone={load} />);
  const revoke = async (r: RoleInfo, user: string) => {
    try { await secApi.grantRole(props.serverId, props.dbId, r.name, { user: `"${user}"`, grant: false }); app.toast(`Revoked ${r.name} from ${user}`); load(); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <>
      <div className="sec-bar">
        <button className="btn primary" onClick={() => edit()}><IconPlus /> Create role</button>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        {data && !data.supported && <span className="warn">System privileges need Firebird 4 or newer (this is Firebird {data.engine_major}).</span>}
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid">
            <thead><tr><th>Role</th><th>Owner</th><th>System privileges</th><th>Granted to</th><th /></tr></thead>
            <tbody>
              {data.roles.map((r) => (
                <tr key={r.name}>
                  <td><b>{r.name}</b>{r.system && <span className="muted"> (system)</span>}{r.description && <div className="muted">{r.description}</div>}</td>
                  <td>{r.owner}</td>
                  <td className="sec-privs">{r.name === "RDB$ADMIN" ? <span className="pill">all</span> : r.privileges.map((p) => <span key={p} className="pill">{p}</span>)}</td>
                  <td>{r.members.map((m) => (
                    <span key={m.user} className="pill tag">{m.user}{m.admin_option ? " (admin option)" : ""}
                      <a className="x" title="Revoke" onClick={() => revoke(r, m.user)}>×</a></span>
                  ))}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {!r.system && data.supported && <><button className="btn small" onClick={() => edit(r)}>Privileges…</button>{" "}</>}
                    <button className="btn small" onClick={() => grant(r)}>Grant…</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function RoleDialog(props: Props & { role?: RoleInfo; all: string[]; supported: boolean; onDone: () => void }) {
  const r = props.role;
  const [name, setName] = useState(r?.name ?? "");
  const [desc, setDesc] = useState("");
  const [privs, setPrivs] = useState<string[]>(r?.privileges ?? []);
  const toggle = (p: string) => setPrivs((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  const ordered = props.all.filter((p) => privs.includes(p));
  const run = (preview: boolean) => r
    ? secApi.alterRole(props.serverId, props.dbId, r.name, ordered, preview)
    : secApi.createRole(props.serverId, props.dbId, { name, privileges: ordered, description: desc || null }, preview);
  return (
    <SqlDialog title={r ? `System privileges of ${r.name}` : "Create role"} run={run} onDone={props.onDone} wide okLabel={r ? "Apply" : "Create role"}>
      {!r && (
        <div className="form" style={{ marginBottom: 12 }}>
          <label className="l">Role name</label>
          <input className="text" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
          <label className="l">Description</label>
          <input className="text" value={desc} onChange={(e) => setDesc(e.target.value)} />
        </div>
      )}
      {props.supported ? (
        <>
          <div className="muted" style={{ marginBottom: 6 }}>SET SYSTEM PRIVILEGES TO … (users get them when the role is active, e.g. granted as DEFAULT)</div>
          <div className="sec-priv-grid">
            {props.all.map((p) => (
              <label key={p} className="check"><input type="checkbox" checked={privs.includes(p)} onChange={() => toggle(p)} /> {p}</label>
            ))}
          </div>
        </>
      ) : <p className="muted">System privileges need Firebird 4 or newer.</p>}
    </SqlDialog>
  );
}

function GrantDialog(props: Props & { role: RoleInfo; onDone: () => void }) {
  const app = useApp();
  const [user, setUser] = useState("");
  const [def, setDef] = useState(true);
  const [adminOpt, setAdminOpt] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = () => app.showDialog(null);
  const go = async () => {
    try {
      await secApi.grantRole(props.serverId, props.dbId, props.role.name, { user, grant: true, default: def, admin_option: adminOpt });
      app.toast(`Granted ${props.role.name} to ${user}`);
      props.onDone();
      close();
    } catch (e) { setError(errMsg(e)); }
  };
  return (
    <Modal title={`Grant ${props.role.name}`} onClose={close} footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>Cancel</button>
      <button className="btn primary" disabled={!user} onClick={go}>Grant</button>
    </>}>
      <div className="form">
        <label className="l">To user</label>
        <input className="text" autoFocus value={user} onChange={(e) => setUser(e.target.value)} />
        <span />
        <label className="check"><input type="checkbox" checked={def} onChange={(e) => setDef(e.target.checked)} /> DEFAULT (active without SET ROLE)</label>
        <span />
        <label className="check"><input type="checkbox" checked={adminOpt} onChange={(e) => setAdminOpt(e.target.checked)} /> WITH ADMIN OPTION</label>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- mappings
const USING_LABEL: Record<string, string> = {
  plugin: "PLUGIN", any_plugin: "ANY PLUGIN", serverwide: "ANY PLUGIN SERVERWIDE", mapping: "MAPPING", all: "*",
};

function MappingsTab(props: Props) {
  const app = useApp();
  const [data, setData] = useState<Awaited<ReturnType<typeof secApi.mappings>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => secApi.mappings(props.serverId, props.dbId).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  const edit = (m?: Mapping, global = false) => app.showDialog(<MappingDialog {...props} mapping={m} global={global} onDone={load} />);
  const drop = async (m: Mapping) => {
    if (!confirm(`Drop ${m.global ? "global " : ""}mapping ${m.name}?`)) return;
    try { await secApi.dropMapping(props.serverId, props.dbId, m.name, m.global); app.toast("Mapping dropped"); load(); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  const table = (rows: Mapping[]) => (
    <div className="grid-wrap" style={{ maxHeight: "none", marginBottom: 14 }}>
      <table className="grid">
        <thead><tr><th>Name</th><th>Using</th><th>In database</th><th>From</th><th>To</th><th>Description</th><th /></tr></thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.name}>
              <td><b>{m.name}</b>{m.system && <span className="muted"> (system)</span>}</td>
              <td>{USING_LABEL[m.using] ?? m.using}{m.plugin ? ` ${m.plugin}` : ""}</td>
              <td>{m.database}</td>
              <td>{m.from_name === "*" ? `ANY ${m.from_type}` : `${m.from_type} ${m.from_name}`}</td>
              <td>{m.to_type} {m.to_name ?? <span className="muted">(same name)</span>}</td>
              <td>{m.description}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                {!m.system && <><button className="btn small" onClick={() => edit(m, m.global)}>Edit</button>{" "}
                  <button className="btn small danger" onClick={() => drop(m)}><IconTrash size={12} /></button></>}
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={7} className="muted">No mappings</td></tr>}
        </tbody>
      </table>
    </div>
  );
  return (
    <>
      <div className="sec-bar">
        <button className="btn primary" onClick={() => edit(undefined, false)}><IconPlus /> Local mapping</button>
        <button className="btn" onClick={() => edit(undefined, true)}><IconPlus /> Global mapping</button>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        {error && <span className="err">{error}</span>}
      </div>
      {data && <>
        <h3>Local mappings <span className="muted">(RDB$AUTH_MAPPING – this database)</span></h3>
        {table(data.local)}
        <h3>Global mappings <span className="muted">(SEC$GLOBAL_AUTH_MAPPING – security database)</span></h3>
        {data.global_error ? <p className="err">{data.global_error}</p> : table(data.global)}
        <p className="muted">Mappings turn an authenticated identity (e.g. a Windows account via Win_Sspi trusted authentication, or a user from another security database) into a Firebird user or role.</p>
      </>}
    </>
  );
}

const FROM_TYPES = ["USER", "GROUP", "PREDEFINED_GROUP", "ROLE"];

function MappingDialog(props: Props & { mapping?: Mapping; global: boolean; onDone: () => void }) {
  const m = props.mapping;
  const [f, setF] = useState({
    name: m?.name ?? "", is_global: m?.global ?? props.global, using: m?.using ?? "plugin", plugin: m?.plugin ?? "Srp",
    database: m?.database ?? "", from_type: m?.from_type || "USER", from_name: m && m.from_name !== "*" ? m.from_name : "",
    any: m ? m.from_name === "*" : false, to_type: m?.to_type ?? "USER", to_name: m?.to_name ?? "", description: m?.description ?? "",
  });
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const body = () => ({
    name: f.name, is_global: f.is_global, using: f.using, plugin: f.using === "plugin" ? f.plugin : null,
    database: f.using !== "serverwide" && f.database ? f.database : null, from_type: f.from_type,
    from_name: f.any ? null : (m ? `"${f.from_name.replace(/"/g, '""')}"` : f.from_name), to_type: f.to_type,
    to_name: f.to_name ? (m ? `"${f.to_name.replace(/"/g, '""')}"` : f.to_name) : null, description: f.description || null,
  });
  const run = (preview: boolean) => m
    ? secApi.alterMapping(props.serverId, props.dbId, m.name, body(), preview)
    : secApi.createMapping(props.serverId, props.dbId, body(), preview);
  return (
    <SqlDialog title={m ? `Alter ${m.global ? "global " : ""}mapping ${m.name}` : `Create ${f.is_global ? "global" : "local"} mapping`} run={run} onDone={props.onDone} okLabel={m ? "Alter mapping" : "Create mapping"}>
      <div className="form">
        <label className="l">Name</label>
        <input className="text" autoFocus={!m} disabled={!!m} value={f.name} onChange={(e) => set("name", e.target.value)} />
        <label className="l">Scope</label>
        <select className="text" disabled={!!m} value={f.is_global ? "global" : "local"} onChange={(e) => set("is_global", e.target.value === "global")}>
          <option value="local">local (this database)</option>
          <option value="global">global (security database, all databases)</option>
        </select>
        <label className="l">Using</label>
        <select className="text" value={f.using} onChange={(e) => set("using", e.target.value)}>
          <option value="plugin">PLUGIN – one authentication plugin</option>
          <option value="any_plugin">ANY PLUGIN</option>
          <option value="serverwide">ANY PLUGIN SERVERWIDE</option>
          <option value="mapping">MAPPING – result of another mapping</option>
          <option value="all">* – any method</option>
        </select>
        {f.using === "plugin" && <>
          <label className="l">Plugin</label>
          <div>
            <input className="text" list="fb-map-plugins" value={f.plugin} onChange={(e) => set("plugin", e.target.value)} />
            <datalist id="fb-map-plugins">{["Srp", "Srp256", "Win_Sspi", "Legacy_Auth"].map((p) => <option key={p} value={p} />)}</datalist>
          </div>
        </>}
        {f.using !== "serverwide" && <>
          <label className="l">In database</label>
          <input className="text" value={f.database} placeholder="optional: where the identity comes from" onChange={(e) => set("database", e.target.value)} />
        </>}
        <label className="l">From</label>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <select className="text" value={f.from_type} onChange={(e) => set("from_type", e.target.value)}>
            {[...new Set([...FROM_TYPES, f.from_type])].map((t) => <option key={t}>{t}</option>)}
          </select>
          <label className="check"><input type="checkbox" checked={f.any} onChange={(e) => set("any", e.target.checked)} /> ANY</label>
          {!f.any && <input className="text" style={{ flex: 1 }} value={f.from_name} placeholder="e.g. DOMAIN_ANY_RID_ADMINS" onChange={(e) => set("from_name", e.target.value)} />}
        </div>
        <label className="l">To</label>
        <div style={{ display: "flex", gap: 6 }}>
          <select className="text" value={f.to_type} onChange={(e) => set("to_type", e.target.value)}>
            <option>USER</option><option>ROLE</option>
          </select>
          <input className="text" style={{ flex: 1 }} value={f.to_name} placeholder={f.to_type === "ROLE" ? "role name, e.g. RDB$ADMIN" : "(empty = same name)"} onChange={(e) => set("to_name", e.target.value)} />
        </div>
        <label className="l">Description</label>
        <input className="text" value={f.description} placeholder="Firebird 5+" onChange={(e) => set("description", e.target.value)} />
      </div>
    </SqlDialog>
  );
}

// ---------------------------------------------------------------- Services API users (server level)
export function SvcUsersPanel(props: { serverId: string }) {
  const app = useApp();
  const [secDb, setSecDb] = useState("");
  const [users, setUsers] = useState<Awaited<ReturnType<typeof secApi.svcUsers>>["users"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => secApi.svcUsers(props.serverId, secDb || undefined).then((r) => { setUsers(r.users); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  const edit = (u?: { user_name: string; first_name: string; middle_name: string; last_name: string; admin: boolean }) =>
    app.showDialog(<SvcUserDialog serverId={props.serverId} securityDb={secDb} user={u} onDone={load} />);
  const drop = async (name: string) => {
    if (!confirm(`Delete user ${name}?`)) return;
    try { await secApi.svcDeleteUser(props.serverId, name, secDb || undefined); app.toast(`User ${name} deleted`); load(); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <div className="pad sec-panel">
      <div className="sec-bar">
        <button className="btn primary" onClick={() => edit()}><IconPlus /> Add user</button>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        <label className="muted">Security database of
          <input className="text" style={{ width: 280, marginLeft: 6 }} value={secDb} placeholder="(default) or a database path/alias"
            onChange={(e) => setSecDb(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
        </label>
        {error && <span className="err">{error}</span>}
      </div>
      <p className="muted">Services API user management (like gsec). It works without a database connection; plugins and tags are managed per database under “Users, roles &amp; mappings”.</p>
      {users && (
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid">
            <thead><tr><th>User</th><th>First name</th><th>Middle name</th><th>Last name</th><th>Admin</th><th /></tr></thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.user_name}>
                  <td><b>{u.user_name}</b></td><td>{u.first_name}</td><td>{u.middle_name}</td><td>{u.last_name}</td><td>{u.admin ? "✓" : ""}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn small" onClick={() => edit(u)}>Edit</button>{" "}
                    <button className="btn small danger" onClick={() => drop(u.user_name)}><IconTrash size={12} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SvcUserDialog(props: { serverId: string; securityDb: string; user?: { user_name: string; first_name: string; middle_name: string; last_name: string; admin: boolean }; onDone: () => void }) {
  const app = useApp();
  const u = props.user;
  const [f, setF] = useState({ name: u?.user_name ?? "", password: "", first_name: u?.first_name ?? "", middle_name: u?.middle_name ?? "", last_name: u?.last_name ?? "", admin: u?.admin ?? false });
  const [error, setError] = useState<string | null>(null);
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const close = () => app.showDialog(null);
  const save = async () => {
    try {
      const body = { ...f, password: f.password || null, security_db: props.securityDb || null };
      if (u) await secApi.svcModifyUser(props.serverId, u.user_name, body);
      else await secApi.svcAddUser(props.serverId, body);
      app.toast(u ? "User modified" : "User added");
      props.onDone();
      close();
    } catch (e) { setError(errMsg(e)); }
  };
  return (
    <Modal title={u ? `Modify user ${u.user_name}` : "Add user (Services API)"} onClose={close} footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>Cancel</button>
      <button className="btn primary" disabled={!f.name || (!u && !f.password)} onClick={save}>{u ? "Save" : "Add"}</button>
    </>}>
      <div className="form">
        <label className="l">User name</label>
        <input className="text" autoFocus={!u} disabled={!!u} value={f.name} onChange={(e) => set("name", e.target.value)} />
        <label className="l">{u ? "New password" : "Password"}</label>
        <input className="text" type="password" autoComplete="new-password" placeholder={u ? "(unchanged)" : ""} value={f.password} onChange={(e) => set("password", e.target.value)} />
        <label className="l">First name</label>
        <input className="text" value={f.first_name} onChange={(e) => set("first_name", e.target.value)} />
        <label className="l">Middle name</label>
        <input className="text" value={f.middle_name} onChange={(e) => set("middle_name", e.target.value)} />
        <label className="l">Last name</label>
        <input className="text" value={f.last_name} onChange={(e) => set("last_name", e.target.value)} />
        <span />
        <label className="check"><input type="checkbox" checked={f.admin} onChange={(e) => set("admin", e.target.checked)} /> Administrator (admin role in the security database)</label>
      </div>
    </Modal>
  );
}

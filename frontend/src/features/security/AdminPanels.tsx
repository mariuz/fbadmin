/** Administrator tabs: fbadmin user accounts and the audit log. */
import { Fragment, useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import { Modal } from "../../components/Modal";
import { IconPlus, IconRefresh } from "../../components/Icons";
import { secApi, type AppUser, type AuditRow } from "./api";
import { useAuth } from "./state";

const fmtTime = (t: number | null) => (t ? new Date(t * 1000).toLocaleString() : "—");

export function UsersAdmin() {
  const app = useApp();
  const auth = useAuth();
  const [users, setUsers] = useState<AppUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = () => secApi.users().then((u) => { setUsers(u); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); app.toast(msg); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const edit = (u?: AppUser) => app.showDialog(<UserDialog user={u} onSaved={load} />);
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn primary" onClick={() => edit()}><IconPlus /> Add user</button>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        {error && <span className="err">{error}</span>}
      </div>
      <div className="pad" style={{ overflow: "auto" }}>
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid">
            <thead><tr><th>User</th><th>E-mail</th><th>Role</th><th>Sign-in</th><th>Status</th><th>Created</th><th>Last login</th><th /></tr></thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td><b>{u.username}</b>{u.id === auth?.user?.id && <span className="muted"> (you)</span>}</td>
                  <td>{u.email}</td>
                  <td><span className={"pill " + (u.role === "admin" ? "running" : "")}>{u.role}</span></td>
                  <td>{u.auth_source}</td>
                  <td>{u.active ? <span className="ok">active</span> : <span className="err">disabled</span>}</td>
                  <td>{fmtTime(u.created_at)}</td>
                  <td>{fmtTime(u.last_login)}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn small" onClick={() => edit(u)}>Edit</button>{" "}
                    <button className="btn small" onClick={() => act(() => secApi.updateUser(u.id, { active: !u.active }), u.active ? "User disabled" : "User enabled")}>
                      {u.active ? "Disable" : "Enable"}
                    </button>{" "}
                    <button className="btn small danger" disabled={u.id === auth?.user?.id}
                      onClick={() => confirm(`Delete fbadmin user ${u.username} and their saved servers?`) && act(() => secApi.deleteUser(u.id), "User deleted")}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted">Users sign in with the sources configured on the server (internal, LDAP, OAuth2/OIDC, reverse proxy). Accounts for external sources are created automatically on first sign-in.</p>
      </div>
    </div>
  );
}

function UserDialog(props: { user?: AppUser; onSaved: () => void }) {
  const app = useApp();
  const u = props.user;
  const [f, setF] = useState({ username: u?.username ?? "", email: u?.email ?? "", password: "", role: u?.role ?? "user",
    active: u?.active ?? true, auth_source: u?.auth_source ?? "internal" });
  const [error, setError] = useState<string | null>(null);
  const set = (k: string, v: unknown) => setF((o) => ({ ...o, [k]: v }));
  const close = () => app.showDialog(null);
  const save = async () => {
    try {
      if (u) await secApi.updateUser(u.id, { email: f.email || null, role: f.role, active: f.active, ...(f.password ? { password: f.password } : {}) });
      else await secApi.createUser({ ...f, email: f.email || null, password: f.auth_source === "internal" ? f.password : null });
      app.toast(u ? "User updated" : "User created");
      props.onSaved();
      close();
    } catch (e) {
      setError(errMsg(e));
    }
  };
  const internal = f.auth_source === "internal";
  return (
    <Modal title={u ? `Edit user – ${u.username}` : "Add fbadmin user"} onClose={close} footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>Cancel</button>
      <button className="btn primary" disabled={!f.username || (!u && internal && f.password.length < 8)} onClick={save}>{u ? "Save" : "Create"}</button>
    </>}>
      <div className="form">
        <label className="l">User name</label>
        <input className="text" autoFocus={!u} disabled={!!u} value={f.username} onChange={(e) => set("username", e.target.value)} />
        <label className="l">E-mail</label>
        <input className="text" type="email" value={f.email} onChange={(e) => set("email", e.target.value)} />
        <label className="l">Sign-in source</label>
        <select className="text" disabled={!!u} value={f.auth_source} onChange={(e) => set("auth_source", e.target.value)}>
          <option value="internal">internal (password stored in fbadmin)</option>
          <option value="ldap">LDAP</option>
          <option value="proxy">reverse proxy</option>
          {f.auth_source.startsWith("oidc:") && <option value={f.auth_source}>{f.auth_source}</option>}
        </select>
        {internal && <>
          <label className="l">{u ? "New password" : "Password"}</label>
          <input className="text" type="password" autoComplete="new-password" placeholder={u ? "(unchanged)" : "8+ characters"} value={f.password} onChange={(e) => set("password", e.target.value)} />
        </>}
        <label className="l">Role</label>
        <select className="text" value={f.role} onChange={(e) => set("role", e.target.value)}>
          <option value="user">user – own servers, shared servers</option>
          <option value="admin">admin – users, shared servers, audit log</option>
        </select>
        <span />
        <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => set("active", e.target.checked)} /> Active</label>
      </div>
      {u && internal && <p className="muted">Setting a new password signs the user out and forgets the Firebird passwords they saved (they are encrypted with their old password).</p>}
    </Modal>
  );
}

const CATS = ["", "DDL", "DML", "TX", "ACTION", "OTHER"];

export function AuditLog(props: { visible: boolean }) {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [total, setTotal] = useState(0);
  const [enabled, setEnabled] = useState(true);
  const [filter, setFilter] = useState({ q: "", username: "", success: "", category: "" });
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const size = 100;
  const load = () =>
    secApi.audit({ limit: size, offset: page * size, q: filter.q, username: filter.username, success: filter.success,
      category: filter.category })
      .then((r) => { setRows(r.rows); setTotal(r.total); setEnabled(r.enabled); setError(null); })
      .catch((e) => setError(errMsg(e)));
  useEffect(() => { if (props.visible) load(); }, [page, props.visible]);
  const shown = rows;
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <input className="text" placeholder="Search statements, errors, databases" style={{ width: 260 }} value={filter.q}
          onChange={(e) => setFilter({ ...filter, q: e.target.value })} onKeyDown={(e) => e.key === "Enter" && (setPage(0), load())} />
        <input className="text" placeholder="User" style={{ width: 120 }} value={filter.username}
          onChange={(e) => setFilter({ ...filter, username: e.target.value })} onKeyDown={(e) => e.key === "Enter" && load()} />
        <select className="text" value={filter.success} onChange={(e) => setFilter({ ...filter, success: e.target.value })}>
          <option value="">all results</option><option value="true">succeeded</option><option value="false">failed</option>
        </select>
        <select className="text" value={filter.category} onChange={(e) => setFilter({ ...filter, category: e.target.value })}>
          {CATS.map((c) => <option key={c} value={c}>{c || "all kinds"}</option>)}
        </select>
        <button className="btn" onClick={() => { setPage(0); load(); }}><IconRefresh /> Apply</button>
        <span className="spacer" />
        <span className="muted">{total.toLocaleString()} entries</span>
        <button className="btn small" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
        <span className="muted">page {page + 1} / {Math.max(1, Math.ceil(total / size))}</span>
        <button className="btn small" disabled={(page + 1) * size >= total} onClick={() => setPage(page + 1)}>›</button>
        {error && <span className="err">{error}</span>}
      </div>
      {!enabled && <div className="pad warn">Auditing is off. Start fbadmin with FBADMIN_AUDIT=1 (it is on by default in server mode).</div>}
      <div style={{ flex: 1, overflow: "auto" }}>
        <table className="grid audit-grid">
          <thead><tr><th>When</th><th>User</th><th>Server</th><th>Database</th><th>Kind</th><th>Statement</th><th>Result</th><th>Rows</th><th>ms</th></tr></thead>
          <tbody>
            {shown.map((r) => (
              <Fragment key={r.id}>
                <tr className={"audit-row" + (r.success ? "" : " failed")} onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <td style={{ whiteSpace: "nowrap" }}>{fmtTime(r.ts)}</td>
                  <td>{r.username}{r.client_ip && <div className="muted small">{r.client_ip}</div>}</td>
                  <td title={r.server_host ?? ""}>{r.server_name ?? r.server_id}</td>
                  <td title={r.db_path ?? ""}>{r.db_name ?? r.db_id ?? ""}</td>
                  <td><span className={"pill cat-" + r.category}>{r.category}</span></td>
                  <td className="audit-sql">{r.statement.split("\n")[0].slice(0, 160)}</td>
                  <td>{r.success ? <span className="ok">ok</span> : <span className="err" title={r.error ?? ""}>error</span>}</td>
                  <td className="num">{r.row_count ?? ""}</td>
                  <td className="num">{r.elapsed_ms ?? ""}</td>
                </tr>
                {open === r.id && (
                  <tr className="audit-detail"><td colSpan={9}>
                    <pre className="log">{r.statement}</pre>
                    {r.error && <pre className="log err">{r.error}</pre>}
                    <div className="muted">{r.server_host} · {r.db_path}</div>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {shown.length === 0 && <div className="empty-state" style={{ height: 200 }}>No audit entries</div>}
      </div>
    </div>
  );
}

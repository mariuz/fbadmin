/** Administrator tabs: fbadmin user accounts and the audit log. */
import { Fragment, useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import { Modal } from "../../components/Modal";
import { IconPlus, IconRefresh } from "../../components/Icons";
import { secApi, type AppUser, type AuditRow } from "./api";
import { useAuth } from "./state";
import { t } from "../../i18n";

const fmtTime = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

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
        <button className="btn primary" onClick={() => edit()}><IconPlus /> {t("Add user")}</button>
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        {error && <span className="err">{error}</span>}
      </div>
      <div className="pad" style={{ overflow: "auto" }}>
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid">
            <thead><tr><th>{t("User")}</th><th>{t("E-mail")}</th><th>{t("Role")}</th><th>{t("Sign-in")}</th><th>{t("Status")}</th><th>{t("Created")}</th><th>{t("Last login")}</th><th /></tr></thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td><b>{u.username}</b>{u.id === auth?.user?.id && <span className="muted"> {t("(you)")}</span>}</td>
                  <td>{u.email}</td>
                  <td><span className={"pill " + (u.role === "admin" ? "running" : "")}>{u.role}</span></td>
                  <td>{u.auth_source}</td>
                  <td>{u.active ? <span className="ok">{t("active")}</span> : <span className="err">{t("disabled")}</span>}</td>
                  <td>{fmtTime(u.created_at)}</td>
                  <td>{fmtTime(u.last_login)}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn small" onClick={() => edit(u)}>{t("Edit")}</button>{" "}
                    <button className="btn small" onClick={() => act(() => secApi.updateUser(u.id, { active: !u.active }), u.active ? t("User disabled") : t("User enabled"))}>
                      {u.active ? t("Disable") : t("Enable")}
                    </button>{" "}
                    <button className="btn small danger" disabled={u.id === auth?.user?.id}
                      onClick={() => confirm(t("Delete fbadmin user {user} and their saved servers?", { user: u.username })) && act(() => secApi.deleteUser(u.id), t("User deleted"))}>{t("Delete")}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted">{t("Users sign in with the sources configured on the server (internal, LDAP, OAuth2/OIDC, reverse proxy). Accounts for external sources are created automatically on first sign-in.")}</p>
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
      app.toast(u ? t("User updated") : t("User created"));
      props.onSaved();
      close();
    } catch (e) {
      setError(errMsg(e));
    }
  };
  const internal = f.auth_source === "internal";
  return (
    <Modal title={u ? t("Edit user – {user}", { user: u.username }) : t("Add fbadmin user")} onClose={close} footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>{t("Cancel")}</button>
      <button className="btn primary" disabled={!f.username || (!u && internal && f.password.length < 8)} onClick={save}>{u ? t("Save") : t("Create")}</button>
    </>}>
      <div className="form">
        <label className="l">{t("User name")}</label>
        <input className="text" autoFocus={!u} disabled={!!u} value={f.username} onChange={(e) => set("username", e.target.value)} />
        <label className="l">{t("E-mail")}</label>
        <input className="text" type="email" value={f.email} onChange={(e) => set("email", e.target.value)} />
        <label className="l">{t("Sign-in source")}</label>
        <select className="text" disabled={!!u} value={f.auth_source} onChange={(e) => set("auth_source", e.target.value)}>
          <option value="internal">{t("internal (password stored in fbadmin)")}</option>
          <option value="ldap">LDAP</option>
          <option value="proxy">{t("reverse proxy")}</option>
          {f.auth_source.startsWith("oidc:") && <option value={f.auth_source}>{f.auth_source}</option>}
        </select>
        {internal && <>
          <label className="l">{u ? t("New password") : t("Password")}</label>
          <input className="text" type="password" autoComplete="new-password" placeholder={u ? t("(unchanged)") : t("8+ characters")} value={f.password} onChange={(e) => set("password", e.target.value)} />
        </>}
        <label className="l">{t("Role")}</label>
        <select className="text" value={f.role} onChange={(e) => set("role", e.target.value)}>
          <option value="user">{t("user – own servers, shared servers")}</option>
          <option value="admin">{t("admin – users, shared servers, audit log")}</option>
        </select>
        <span />
        <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => set("active", e.target.checked)} /> {t("Active")}</label>
      </div>
      {u && internal && <p className="muted">{t("Setting a new password signs the user out and forgets the Firebird passwords they saved (they are encrypted with their old password).")}</p>}
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
        <input className="text" placeholder={t("Search statements, errors, databases")} style={{ width: 260 }} value={filter.q}
          onChange={(e) => setFilter({ ...filter, q: e.target.value })} onKeyDown={(e) => e.key === "Enter" && (setPage(0), load())} />
        <input className="text" placeholder={t("User")} style={{ width: 120 }} value={filter.username}
          onChange={(e) => setFilter({ ...filter, username: e.target.value })} onKeyDown={(e) => e.key === "Enter" && load()} />
        <select className="text" value={filter.success} onChange={(e) => setFilter({ ...filter, success: e.target.value })}>
          <option value="">{t("all results")}</option><option value="true">{t("succeeded")}</option><option value="false">{t("failed")}</option>
        </select>
        <select className="text" value={filter.category} onChange={(e) => setFilter({ ...filter, category: e.target.value })}>
          {CATS.map((c) => <option key={c} value={c}>{c || t("all kinds")}</option>)}
        </select>
        <button className="btn" onClick={() => { setPage(0); load(); }}><IconRefresh /> {t("Apply")}</button>
        <span className="spacer" />
        <span className="muted">{t("{n} entries", { n: total.toLocaleString() })}</span>
        <button className="btn small" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
        <span className="muted">{t("page {page} / {pages}", { page: page + 1, pages: Math.max(1, Math.ceil(total / size)) })}</span>
        <button className="btn small" disabled={(page + 1) * size >= total} onClick={() => setPage(page + 1)}>›</button>
        {error && <span className="err">{error}</span>}
      </div>
      {!enabled && <div className="pad warn">{t("Auditing is off. Start fbadmin with FBADMIN_AUDIT=1 (it is on by default in server mode).")}</div>}
      <div style={{ flex: 1, overflow: "auto" }}>
        <table className="grid audit-grid">
          <thead><tr><th>{t("When")}</th><th>{t("User")}</th><th>{t("Server")}</th><th>{t("Database")}</th><th>{t("Kind")}</th><th>{t("Statement")}</th><th>{t("Result")}</th><th>{t("Rows")}</th><th>ms</th></tr></thead>
          <tbody>
            {shown.map((r) => (
              <Fragment key={r.id}>
                <tr className={"audit-row" + (r.success ? "" : " failed")} onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <td style={{ whiteSpace: "nowrap" }}>{fmtTime(r.ts)}</td>
                  <td>{r.username}{r.token && <div className="muted small">{t("token: {name}", { name: r.token })}</div>}{r.client_ip && <div className="muted small">{r.client_ip}</div>}</td>
                  <td title={r.server_host ?? ""}>{r.server_name ?? r.server_id}</td>
                  <td title={r.db_path ?? ""}>{r.db_name ?? r.db_id ?? ""}</td>
                  <td><span className={"pill cat-" + r.category}>{r.category}</span></td>
                  <td className="audit-sql">{r.statement.split("\n")[0].slice(0, 160)}</td>
                  <td>{r.success ? <span className="ok">{t("ok")}</span> : <span className="err" title={r.error ?? ""}>{t("error")}</span>}</td>
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
        {shown.length === 0 && <div className="empty-state" style={{ height: 200 }}>{t("No audit entries")}</div>}
      </div>
    </div>
  );
}

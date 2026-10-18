/** Top-bar button: signed-in user, master password, user admin, audit log, sign out. */
import { useEffect, useRef, useState } from "react";
import type { AppApi } from "../../context";
import { errMsg } from "../../context";
import { Modal } from "../../components/Modal";
import { IconUser } from "../../components/Icons";
import { secApi } from "./api";
import { isAdmin, refreshAuth, setAuth, useAuth } from "./state";
import { VaultDialog } from "./VaultDialog";

export function UserMenu(props: { app: AppApi }) {
  const { app } = props;
  const auth = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  if (!auth) return null;
  const vault = auth.vault;
  const locked = !!vault?.enabled && !vault.unlocked;
  const item = (label: string, fn: () => void, cls = "") => (
    <button className={cls} onClick={() => { setOpen(false); fn(); }}>{label}</button>
  );
  const label = auth.server_mode ? auth.user?.username : "Security";

  return (
    <div className="user-menu" ref={ref}>
      <button className="tb" onClick={() => setOpen(!open)} title={auth.server_mode ? `Signed in as ${auth.user?.username}` : "Security"}>
        <IconUser /> {label}{locked && <span className="badge" title="Saved passwords are locked">locked</span>}
      </button>
      {open && (
        <div className="ctx user-menu-pop">
          {auth.server_mode && (
            <div className="sub-h">{auth.user?.email || auth.user?.username} · {auth.user?.role} · {auth.user?.auth_source}</div>
          )}
          {item(vault?.enabled ? (locked ? "Unlock saved passwords…" : "Master password…") : "Set master password…",
            () => app.showDialog(<VaultDialog manage onDone={(ok) => { app.showDialog(null); if (ok) { app.toast("Master password updated"); app.reloadServers(); } }} />))}
          {vault?.enabled && !locked && !vault.login_bound && item("Lock saved passwords", async () => {
            await secApi.vaultLock();
            await refreshAuth();
            app.toast("Saved passwords locked", "info");
          })}
          {auth.server_mode && auth.user?.auth_source === "internal" && item("Change password…", () => app.showDialog(<ChangePasswordDialog app={app} />))}
          {isAdmin(auth) && <hr />}
          {isAdmin(auth) && auth.server_mode && item("Manage users", () => app.openTab({ kind: "security-users", title: "fbadmin users" }))}
          {isAdmin(auth) && item("Audit log", () => app.openTab({ kind: "security-audit", title: "Audit log" }))}
          {auth.server_mode && <hr />}
          {auth.server_mode && item("Sign out", async () => {
            try { await secApi.logout(); } catch { /* ignore */ }
            setAuth({ ...auth, authenticated: false, user: null });
            refreshAuth().catch(() => undefined);
          }, "danger")}
        </div>
      )}
    </div>
  );
}

function ChangePasswordDialog(props: { app: AppApi }) {
  const [f, setF] = useState({ old: "", pw: "", pw2: "" });
  const [error, setError] = useState<string | null>(null);
  const close = () => props.app.showDialog(null);
  const submit = async () => {
    if (f.pw !== f.pw2) return setError("The passwords do not match");
    try {
      await secApi.changePassword(f.old, f.pw);
      props.app.toast("Password changed");
      close();
    } catch (e) {
      setError(errMsg(e));
    }
  };
  return (
    <Modal title="Change password" onClose={close} footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>Cancel</button>
      <button className="btn primary" disabled={!f.old || f.pw.length < 8} onClick={submit}>Change</button>
    </>}>
      <div className="form">
        <label className="l">Current password</label>
        <input className="text" type="password" autoFocus value={f.old} onChange={(e) => setF({ ...f, old: e.target.value })} />
        <label className="l">New password</label>
        <input className="text" type="password" value={f.pw} onChange={(e) => setF({ ...f, pw: e.target.value })} />
        <label className="l">Confirm</label>
        <input className="text" type="password" value={f.pw2} onChange={(e) => setF({ ...f, pw2: e.target.value })} />
      </div>
      <p className="muted">Your saved Firebird passwords are protected by this password and stay available.</p>
    </Modal>
  );
}

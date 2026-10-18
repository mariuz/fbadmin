/**
 * Wraps the app: in server mode shows the login (or first-run setup) page until the user is signed in,
 * and answers "auth_required" / "master_password_required" API errors.
 * In desktop mode it renders the app right away (only the master-password prompt applies).
 */
import { useEffect, useState, type ReactNode } from "react";
import { setErrorHandler } from "../../api";
import { errMsg } from "../../context";
import { Flame } from "../../components/Icons";
import { secApi } from "./api";
import { refreshAuth, setAuth, useAuth } from "./state";
import { VaultDialog } from "./VaultDialog";
import { t } from "../../i18n";

let showVault: ((resolve: (ok: boolean) => void) => void) | null = null;

/** Ask for the master password (or to set one); resolves true when saved passwords can be used. */
export function promptVault(): Promise<boolean> {
  return new Promise((resolve) => (showVault ? showVault(resolve) : resolve(false)));
}

export function AuthGate(props: { children: ReactNode }) {
  const auth = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [vault, setVault] = useState<((ok: boolean) => void) | null>(null);

  useEffect(() => {
    refreshAuth().catch((e) => setError(errMsg(e)));
    setErrorHandler("auth_required", async () => {
      await refreshAuth().catch(() => setAuth(null));
      return false;
    });
    setErrorHandler("csrf_failed", async () => {
      await refreshAuth().catch(() => undefined);
      return true; // the new session cookie carries a fresh token
    });
    setErrorHandler("master_password_required", () => promptVault());
    showVault = (resolve) => setVault(() => resolve);
    return () => { showVault = null; };
  }, []);

  if (error && !auth) {
    return <div className="auth-page"><div className="auth-card"><Brand /><p className="err">{error}</p>
      <button className="btn" onClick={() => location.reload()}>{t("Retry")}</button></div></div>;
  }
  if (!auth) return <div className="auth-page"><div className="muted">{t("Loading…")}</div></div>;
  if (auth.server_mode && auth.needs_setup) return <SetupPage />;
  if (auth.server_mode && !auth.authenticated) return <LoginPage />;
  return (
    <>
      {props.children}
      {vault && <VaultDialog onDone={(ok) => { vault(ok); setVault(null); refreshAuth().catch(() => undefined); }} />}
    </>
  );
}

function Brand() {
  return <div className="auth-brand"><Flame size={30} /><div><b>fbadmin</b><small>{t("Firebird administration")}</small></div></div>;
}

function LoginPage() {
  const auth = useAuth()!;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(() => new URLSearchParams(location.search).get("login_error"));
  const password_ok = auth.auth_sources.some((s) => s.type === "password");
  const oidc = auth.auth_sources.filter((s) => s.type === "oidc");
  const proxyOnly = auth.auth_sources.every((s) => s.type === "proxy");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await secApi.login(username, password);
      if (location.search) history.replaceState(null, "", location.pathname);
      setAuth(r);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <Brand />
        <h2>{t("Sign in")}</h2>
        {password_ok && (
          <>
            <label>{t("User name or e-mail")}
              <input className="text" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
            </label>
            <label>{t("Password")}
              <input className="text" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </label>
            <button className="btn primary" disabled={busy || !username || !password} type="submit">{busy ? t("Signing in…") : t("Sign in")}</button>
          </>
        )}
        {oidc.length > 0 && password_ok && <div className="auth-or"><span>{t("or")}</span></div>}
        {oidc.map((o) => (
          <a key={o.name} className="btn auth-sso" href={`/api/auth/oidc/${encodeURIComponent(o.name)}/login`}>{t("Continue with {name}", { name: o.title || o.name })}</a>
        ))}
        {proxyOnly && <p className="muted">{t("Sign-in is handled by the reverse proxy in front of fbadmin. Reload the page after signing in there.")}</p>}
        {error && <p className="err" role="alert">{error}</p>}
      </form>
    </div>
  );
}

function SetupPage() {
  const [f, setF] = useState({ username: "", email: "", password: "", confirm: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: string, v: string) => setF((o) => ({ ...o, [k]: v }));
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (f.password !== f.confirm) return setError(t("The passwords do not match"));
    setBusy(true);
    setError(null);
    try {
      setAuth(await secApi.setup({ username: f.username, email: f.email || undefined, password: f.password }));
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <Brand />
        <h2>{t("Welcome – create the administrator")}</h2>
        <p className="muted">{t("fbadmin runs in server mode. This first account manages users, shared servers and the audit log.")}</p>
        <label>{t("User name")}<input className="text" autoFocus value={f.username} onChange={(e) => set("username", e.target.value)} /></label>
        <label>{t("E-mail (optional)")}<input className="text" type="email" value={f.email} onChange={(e) => set("email", e.target.value)} /></label>
        <label>{t("Password (8+ characters)")}<input className="text" type="password" autoComplete="new-password" value={f.password} onChange={(e) => set("password", e.target.value)} /></label>
        <label>{t("Confirm password")}<input className="text" type="password" autoComplete="new-password" value={f.confirm} onChange={(e) => set("confirm", e.target.value)} /></label>
        <button className="btn primary" type="submit" disabled={busy || !f.username || f.password.length < 8}>{t("Create administrator")}</button>
        {error && <p className="err" role="alert">{error}</p>}
      </form>
    </div>
  );
}

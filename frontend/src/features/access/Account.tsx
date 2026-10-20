/** "My sign-in & access" tab: two-factor authentication, personal API tokens and effective rights. */
import { useEffect, useState } from "react";
import { useApp } from "../../context";
import { t } from "../../i18n";
import { IconCopy, IconPlus, IconRefresh } from "../../components/Icons";
import { Section, useBusy } from "../maintenance/common";
import { useAuth } from "../security/state";
import { accessApi, type ApiToken, type Effective, type Enrolment, type MfaStatus } from "./api";
import { CodeField, EnrolmentInfo, RecoveryCodes } from "./Mfa";
import { LevelPill, fmtTime } from "./common";

export function AccountPanel(props: { visible: boolean }) {
  return (
    <div className="mt-body access">
      <TwoFactorSection />
      <TokensSection visible={props.visible} />
      <MyAccessSection visible={props.visible} />
    </div>
  );
}

function TwoFactorSection() {
  const auth = useAuth();
  const { busy, error, setError, run } = useBusy();
  const [st, setSt] = useState<MfaStatus | null>(null);
  const [enrol, setEnrol] = useState<Enrolment | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [action, setAction] = useState<"disable" | "recovery" | null>(null);
  const load = () => accessApi.mfa().then(setSt).catch(() => setSt(null));
  useEffect(() => { load(); }, []);
  const source = auth?.user?.auth_source ?? "";

  if (st && !st.available) {
    return (
      <Section title={t("Two-factor authentication")}>
        <div className="muted">{t("You sign in through {source}. A second factor for this account is configured at that identity provider, not in fbadmin.", { source })}</div>
      </Section>
    );
  }
  const start = () => run(async () => { setCodes(null); setCode(""); setEnrol(await accessApi.mfaSetup()); });
  const confirm = () => run(async () => {
    const r = await accessApi.mfaEnable(code);
    setEnrol(null); setCode(""); setCodes(r.recovery_codes); setSt(r);
  }, t("Two-factor authentication is on"));
  const finish = () => run(async () => {
    if (action === "disable") { setSt(await accessApi.mfaDisable(code)); setCodes(null); }
    else if (action === "recovery") { const r = await accessApi.mfaRecovery(code); setSt(r); setCodes(r.recovery_codes); }
    setAction(null); setCode("");
  }, action === "disable" ? t("Two-factor authentication is off") : t("New recovery codes created"));

  return (
    <Section title={t("Two-factor authentication")}
      hint={t("Sign in with your password and a code from an authenticator app on your phone. Codes change every 30 seconds and work once.")}>
      {st && (
        <div className="mt-row" style={{ marginBottom: 8 }}>
          {st.enabled ? <span className="pill running">{t("on")}</span> : <span className="pill">{t("off")}</span>}
          {st.enabled && <span className="muted">{t("{n} recovery codes left", { n: st.recovery_codes_left })}</span>}
          {st.required && <span className="muted">{t("Required by an administrator")}</span>}
        </div>
      )}
      {codes && <RecoveryCodes codes={codes} />}
      {st && !st.enabled && !enrol && (
        <button className="btn primary" disabled={busy} onClick={start}>{t("Set up two-factor authentication")}</button>
      )}
      {enrol && (
        <div className="mfa-box">
          <EnrolmentInfo enrol={enrol} />
          <div className="mt-row">
            <CodeField value={code} onChange={setCode} label={t("Code shown by the app")} />
            <button className="btn primary" disabled={busy || code.length !== 6} onClick={confirm}>{t("Turn on")}</button>
            <button className="btn" onClick={() => { setEnrol(null); setError(null); }}>{t("Cancel")}</button>
          </div>
        </div>
      )}
      {st?.enabled && !action && (
        <div className="mt-row">
          <button className="btn" onClick={() => { setAction("recovery"); setCode(""); }}>{t("New recovery codes…")}</button>
          {!st.required && <button className="btn danger" onClick={() => { setAction("disable"); setCode(""); }}>{t("Turn off…")}</button>}
        </div>
      )}
      {action && (
        <div className="mt-row">
          <CodeField value={code} onChange={setCode} recovery label={t("Current code or a recovery code")} />
          <button className={"btn " + (action === "disable" ? "danger" : "primary")} disabled={busy || code.length < 6} onClick={finish}>
            {action === "disable" ? t("Turn off") : t("Create new codes")}
          </button>
          <button className="btn" onClick={() => setAction(null)}>{t("Cancel")}</button>
        </div>
      )}
      {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
    </Section>
  );
}

function TokensSection(props: { visible: boolean }) {
  const app = useApp();
  const { busy, error, run } = useBusy();
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [form, setForm] = useState<{ name: string; scope: string; expires: string; servers: string[] } | null>(null);
  const [created, setCreated] = useState<{ token: string; name: string; vault: boolean } | null>(null);
  const load = () => accessApi.tokens().then(setTokens).catch(() => undefined);
  useEffect(() => { if (props.visible) load(); }, [props.visible]);
  const servers = app.servers.filter((s) => !s.embedded);
  const create = () => run(async () => {
    if (!form) return;
    const r = await accessApi.createToken({ name: form.name, scope: form.scope, expires_days: form.expires ? Number(form.expires) : null,
      server_ids: form.servers.length ? form.servers : null });
    setCreated({ token: r.token, name: r.name, vault: r.vault });
    setForm(null);
    await load();
  });
  const serverName = (id: string) => servers.find((s) => s.id === id)?.name ?? id;
  return (
    <Section wide title={t("API tokens")}
      hint={t("Personal tokens let scripts call the fbadmin API as you: send the header \"Authorization: Bearer <token>\". A token never does more than you may, and read-only tokens only read.")}
      right={<>
        <button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>
        <button className="btn small primary" onClick={() => { setCreated(null); setForm({ name: "", scope: "read-only", expires: "90", servers: [] }); }}>
          <IconPlus size={12} /> {t("New token")}
        </button>
      </>}>
      {created && (
        <div className="token-created" role="status">
          <p><b>{t("Copy the token \"{name}\" now - it is not shown again.", { name: created.name })}</b></p>
          <div className="mt-row">
            <code className="token-value" data-testid="new-token">{created.token}</code>
            <button className="btn small" onClick={() => { navigator.clipboard?.writeText(created.token).catch(() => undefined); app.toast(t("Copied")); }}>
              <IconCopy size={12} /> {t("Copy")}
            </button>
          </div>
          {!created.vault && <p className="muted small">{t("Your saved Firebird passwords were locked, so this token cannot use them; scripts must send the server password when they connect.")}</p>}
        </div>
      )}
      {form && (
        <div className="form mform" style={{ marginBottom: 12 }}>
          <label className="l" htmlFor="tok-name">{t("Name")}</label>
          <input id="tok-name" className="text" autoFocus value={form.name} placeholder={t("e.g. nightly report")} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <label className="l" htmlFor="tok-scope">{t("Scope")}</label>
          <select id="tok-scope" className="text" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}>
            <option value="read-only">{t("read-only (queries, metadata, monitoring)")}</option>
            <option value="read-write">{t("read-write (everything you may do)")}</option>
          </select>
          <label className="l" htmlFor="tok-exp">{t("Expires after")}</label>
          <select id="tok-exp" className="text" value={form.expires} onChange={(e) => setForm({ ...form, expires: e.target.value })}>
            {["7", "30", "90", "365"].map((d) => <option key={d} value={d}>{t("{n} days", { n: d })}</option>)}
            <option value="">{t("never")}</option>
          </select>
          <span className="l">{t("Servers")}</span>
          <div className="token-servers" role="group" aria-label={t("Servers")}>
            <span className="muted small">{form.servers.length ? "" : t("All your servers (tick some to limit the token)")}</span>
            {servers.map((s) => (
              <label key={s.id} className="check">
                <input type="checkbox" checked={form.servers.includes(s.id)}
                  onChange={(e) => setForm({ ...form, servers: e.target.checked ? [...form.servers, s.id] : form.servers.filter((x) => x !== s.id) })} />
                {s.name}
              </label>
            ))}
          </div>
          <span />
          <div className="mt-row">
            <button className="btn primary" disabled={busy || !form.name.trim()} onClick={create}>{t("Create token")}</button>
            <button className="btn" onClick={() => setForm(null)}>{t("Cancel")}</button>
          </div>
        </div>
      )}
      {tokens.length === 0 ? <div className="muted">{t("No tokens yet.")}</div> : (
        <div className="grid-wrap"><table className="grid">
          <thead><tr><th>{t("Name")}</th><th>{t("Token")}</th><th>{t("Scope")}</th><th>{t("Servers")}</th><th>{t("Created")}</th><th>{t("Expires")}</th><th>{t("Last used")}</th><th>{t("Status")}</th><th /></tr></thead>
          <tbody>{tokens.map((tk) => (
            <tr key={tk.id}>
              <td>{tk.name}</td><td><code>{tk.hint}</code></td>
              <td>{tk.scope === "read-only" ? t("read-only") : t("read-write")}</td>
              <td>{tk.server_ids ? tk.server_ids.map(serverName).join(", ") : t("all")}</td>
              <td>{fmtTime(tk.created_at)}</td><td>{tk.expires_at ? fmtTime(tk.expires_at) : t("never")}</td>
              <td>{tk.last_used_at ? `${fmtTime(tk.last_used_at)}${tk.last_used_ip ? ` · ${tk.last_used_ip}` : ""}` : "—"}</td>
              <td><TokenStatus status={tk.status} /></td>
              <td>{tk.status === "active" && (
                <button className="btn small danger" onClick={() => confirm(t("Revoke token \"{name}\"? Scripts using it stop working.", { name: tk.name }))
                  && run(async () => { await accessApi.revokeToken(tk.id); await load(); }, t("Token revoked"))}>{t("Revoke")}</button>
              )}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
    </Section>
  );
}

export function TokenStatus(props: { status: ApiToken["status"] }) {
  if (props.status === "active") return <span className="ok">{t("active")}</span>;
  return <span className="muted">{props.status === "expired" ? t("expired") : t("revoked")}</span>;
}

function MyAccessSection(props: { visible: boolean }) {
  const [eff, setEff] = useState<Effective | null>(null);
  useEffect(() => { if (props.visible) accessApi.me().then(setEff).catch(() => undefined); }, [props.visible]);
  return (
    <Section wide title={t("My access")} hint={t("What fbadmin lets you do on each server and database. Firebird's own users and GRANTs still apply on top.")}>
      {eff && <EffectiveTable eff={eff} />}
    </Section>
  );
}

export function EffectiveTable(props: { eff: Effective }) {
  const { eff } = props;
  if (eff.admin) return <div className="muted">{t("Administrators have full access to every server.")}</div>;
  if (!eff.restricted) {
    return <div className="muted" data-testid="access-unrestricted">{t("No permissions are defined for this account: it has full access to the servers it can see (as in earlier versions).")}</div>;
  }
  if (!eff.servers.length) return <div className="muted">{t("No access to any server.")}</div>;
  return (
    <div className="grid-wrap"><table className="grid access-effective">
      <thead><tr><th>{t("Server")}</th><th>{t("Database")}</th><th>{t("Level")}</th><th>{t("Objects")}</th></tr></thead>
      <tbody>{eff.servers.flatMap((s) => [
        <tr key={s.server_id}><td><b>{s.server_name}</b>{s.read_only && <span className="muted small"> {t("(read-only server)")}</span>}</td><td className="muted">{t("server-wide")}</td><td><LevelPill level={s.level} /></td><td /></tr>,
        ...s.databases.map((d) => (
          <tr key={s.server_id + d.db_id}>
            <td />
            <td>{d.name}</td>
            <td><LevelPill level={d.level} /></td>
            <td>{d.objects.map((o) => <span key={o.pattern} className="obj-rule"><code>{o.pattern}</code> <LevelPill level={o.level} /></span>)}</td>
          </tr>
        )),
      ])}</tbody>
    </table></div>
  );
}

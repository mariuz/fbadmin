/** Administrators: permissions (grants, groups, default), two-factor policy and every user's API tokens. */
import { useEffect, useState } from "react";
import { useApp } from "../../context";
import { t } from "../../i18n";
import { IconPlus, IconRefresh, IconTrash } from "../../components/Icons";
import { Section, useBusy } from "../maintenance/common";
import { secApi, type AppUser } from "../security/api";
import { accessApi, type AccessOverview, type ApiToken, type Effective, type Grant, type Level, type MfaOverview } from "./api";
import { EffectiveTable, TokenStatus } from "./Account";
import { LEVEL_HELP, LEVEL_LABELS, LEVELS, LevelPill, fmtTime } from "./common";

interface GrantForm { subject: string; server_id: string; db_id: string; object_pattern: string; level: Level; note: string }

export function AdminPanel(props: { visible: boolean }) {
  const app = useApp();
  const { busy, error, run } = useBusy();
  const [ov, setOv] = useState<AccessOverview | null>(null);
  const [users, setUsers] = useState<AppUser[]>([]);
  const [form, setForm] = useState<GrantForm | null>(null);
  const [group, setGroup] = useState<{ name: string; description: string; members: number[]; isNew: boolean } | null>(null);
  const [preview, setPreview] = useState<{ id: string; eff: Effective | null }>({ id: "", eff: null });
  const load = async () => {
    const [o, u] = await Promise.all([accessApi.access(), secApi.users()]);
    setOv(o);
    setUsers(u);
  };
  useEffect(() => { if (props.visible) run(load); }, [props.visible]);
  useEffect(() => {
    if (preview.id) accessApi.effectiveFor(Number(preview.id)).then((eff) => setPreview((p) => ({ ...p, eff }))).catch(() => undefined);
  }, [preview.id, ov]);

  const shared = app.servers.filter((s) => s.shared);
  const srvName = (id: string) => (id === "*" ? t("every server") : shared.find((s) => s.id === id)?.name ?? id);
  const dbName = (sid: string, dbid: string | null) => {
    if (!dbid) return t("all databases");
    return shared.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? dbid;
  };
  const userName = (id: string | number) => users.find((u) => String(u.id) === String(id))?.username ?? `#${id}`;
  const subjectLabel = (g: Grant) => (g.subject_type === "user" ? userName(g.subject) : t("group {name}", { name: g.subject }));
  const formServer = shared.find((s) => s.id === form?.server_id);

  const saveGrant = () => run(async () => {
    if (!form) return;
    const [kind, ...rest] = form.subject.split(":");
    await accessApi.addGrant({
      subject_type: kind as "user" | "group", subject: rest.join(":"), server_id: form.server_id || "*",
      db_id: form.server_id !== "*" && form.db_id ? form.db_id : null, object_pattern: form.object_pattern.trim() || null,
      level: form.level, note: form.note || null,
    });
    setForm(null);
    await load();
  }, t("Grant added"));

  const nonAdmins = users.filter((u) => u.role !== "admin");

  return (
    <div className="mt-body access">
      <Section wide title={t("Permissions")}
        hint={t("Give fbadmin users and groups a level on a shared server, one of its databases, or objects whose names match a pattern. The most specific grant wins. Administrators always have full access.")}
        right={<button className="btn small" onClick={() => run(load)} aria-label={t("Refresh")}><IconRefresh size={12} /></button>}>
        {ov && (
          <div className="form mform" style={{ marginBottom: 12 }}>
            <label className="l" htmlFor="acc-default">{t("Users without grants")}</label>
            <select id="acc-default" className="text" value={ov.default}
              onChange={(e) => run(async () => setOv(await accessApi.accessDefault(e.target.value as "full" | "none")), t("Saved"))}>
              <option value="full">{t("full access to the servers they can see (as before)")}</option>
              <option value="none">{t("no access until they get a grant")}</option>
            </select>
          </div>
        )}
        <div className="level-legend">
          {LEVELS.map((l) => <span key={l}><LevelPill level={l} /> <span className="muted small">{t(LEVEL_HELP[l])}</span></span>)}
        </div>
        <div className="mt-row" style={{ margin: "10px 0" }}>
          <button className="btn small primary" disabled={!users.length}
            onClick={() => setForm({ subject: nonAdmins[0] ? `user:${nonAdmins[0].id}` : "", server_id: shared[0]?.id ?? "*", db_id: "",
              object_pattern: "", level: "read", note: "" })}>
            <IconPlus size={12} /> {t("New grant")}
          </button>
        </div>
        {form && (
          <div className="form mform grant-form" style={{ marginBottom: 12 }}>
            <label className="l" htmlFor="gr-subject">{t("Who")}</label>
            <select id="gr-subject" className="text" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })}>
              <option value="" disabled>{t("Choose a user or group")}</option>
              <optgroup label={t("Users")}>
                {nonAdmins.map((u) => <option key={u.id} value={`user:${u.id}`}>{u.username}</option>)}
              </optgroup>
              {!!ov?.groups.length && <optgroup label={t("Groups")}>
                {ov.groups.map((g) => <option key={g.name} value={`group:${g.name}`}>{g.name}</option>)}
              </optgroup>}
            </select>
            <label className="l" htmlFor="gr-server">{t("Server")}</label>
            <select id="gr-server" className="text" value={form.server_id} onChange={(e) => setForm({ ...form, server_id: e.target.value, db_id: "" })}>
              <option value="*">{t("every server (*)")}</option>
              {shared.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <label className="l" htmlFor="gr-db">{t("Database")}</label>
            <select id="gr-db" className="text" value={form.db_id} disabled={form.server_id === "*"} onChange={(e) => setForm({ ...form, db_id: e.target.value })}>
              <option value="">{t("all databases")}</option>
              {formServer?.databases.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
            <label className="l" htmlFor="gr-obj">{t("Objects (optional)")}</label>
            <input id="gr-obj" className="text" value={form.object_pattern} placeholder={t("e.g. SALARY* or CUSTOMER")}
              onChange={(e) => setForm({ ...form, object_pattern: e.target.value })} />
            <label className="l" htmlFor="gr-level">{t("Level")}</label>
            <select id="gr-level" className="text" value={form.level} onChange={(e) => setForm({ ...form, level: e.target.value as Level })}>
              {LEVELS.map((l) => <option key={l} value={l}>{t(LEVEL_LABELS[l])} – {t(LEVEL_HELP[l])}</option>)}
            </select>
            <label className="l" htmlFor="gr-note">{t("Note")}</label>
            <input id="gr-note" className="text" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
            <span />
            <div className="mt-row">
              <button className="btn primary" disabled={busy || !form.subject} onClick={saveGrant}>{t("Add grant")}</button>
              <button className="btn" onClick={() => setForm(null)}>{t("Cancel")}</button>
            </div>
          </div>
        )}
        {ov && (ov.grants.length === 0 ? <div className="muted">{t("No grants: every user works as in earlier versions.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Who")}</th><th>{t("Server")}</th><th>{t("Database")}</th><th>{t("Objects")}</th><th>{t("Level")}</th><th>{t("Note")}</th><th /></tr></thead>
            <tbody>{ov.grants.map((g) => (
              <tr key={g.id}>
                <td>{subjectLabel(g)}</td><td>{srvName(g.server_id)}</td><td>{dbName(g.server_id, g.db_id)}</td>
                <td>{g.object_pattern ? <code>{g.object_pattern}</code> : <span className="muted">{t("all")}</span>}</td>
                <td><LevelPill level={g.level} /></td><td>{g.note}</td>
                <td><button className="btn small danger" aria-label={t("Delete grant")}
                  onClick={() => run(async () => { await accessApi.deleteGrant(g.id); await load(); }, t("Grant deleted"))}><IconTrash size={12} /></button></td>
              </tr>
            ))}</tbody>
          </table></div>
        ))}
        {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
      </Section>

      <Section wide title={t("Groups")} hint={t("Groups of fbadmin users, to give several users the same grants.")}
        right={<button className="btn small primary" onClick={() => setGroup({ name: "", description: "", members: [], isNew: true })}><IconPlus size={12} /> {t("New group")}</button>}>
        {group && (
          <div className="form mform" style={{ marginBottom: 12 }}>
            <label className="l" htmlFor="grp-name">{t("Name")}</label>
            <input id="grp-name" className="text" value={group.name} disabled={!group.isNew} onChange={(e) => setGroup({ ...group, name: e.target.value })} />
            <label className="l" htmlFor="grp-desc">{t("Description")}</label>
            <input id="grp-desc" className="text" value={group.description} onChange={(e) => setGroup({ ...group, description: e.target.value })} />
            <span className="l">{t("Members")}</span>
            <div className="token-servers" role="group" aria-label={t("Members")}>
              {nonAdmins.map((u) => (
                <label key={u.id} className="check">
                  <input type="checkbox" checked={group.members.includes(u.id)}
                    onChange={(e) => setGroup({ ...group, members: e.target.checked ? [...group.members, u.id] : group.members.filter((x) => x !== u.id) })} />
                  {u.username}
                </label>
              ))}
            </div>
            <span />
            <div className="mt-row">
              <button className="btn primary" disabled={busy || !group.name.trim()} onClick={() => run(async () => {
                await accessApi.saveGroup(group.name.trim(), group.description || null, group.members);
                setGroup(null);
                await load();
              }, t("Group saved"))}>{t("Save")}</button>
              <button className="btn" onClick={() => setGroup(null)}>{t("Cancel")}</button>
            </div>
          </div>
        )}
        {ov && (ov.groups.length === 0 ? <div className="muted">{t("No groups yet.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Description")}</th><th>{t("Members")}</th><th /></tr></thead>
            <tbody>{ov.groups.map((g) => (
              <tr key={g.name}>
                <td>{g.name}</td><td>{g.description}</td><td>{g.members.map(userName).join(", ")}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn small" onClick={() => setGroup({ name: g.name, description: g.description ?? "", members: g.members, isNew: false })}>{t("Edit")}</button>{" "}
                  <button className="btn small danger" aria-label={t("Delete group")} onClick={() => confirm(t("Delete group {name} and its grants?", { name: g.name }))
                    && run(async () => { await accessApi.deleteGroup(g.name); await load(); }, t("Group deleted"))}><IconTrash size={12} /></button>
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        ))}
      </Section>

      <Section wide title={t("Effective rights")} hint={t("What a user may do on the shared servers, after all grants and groups are applied.")}>
        <div className="form mform" style={{ marginBottom: 10 }}>
          <label className="l" htmlFor="eff-user">{t("User")}</label>
          <select id="eff-user" className="text" value={preview.id} onChange={(e) => setPreview({ id: e.target.value, eff: null })}>
            <option value="">{t("Choose a user")}</option>
            {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
          </select>
        </div>
        {preview.eff && <EffectiveTable eff={preview.eff} />}
      </Section>

      <TwoFactorAdmin visible={props.visible} />
      <AllTokens visible={props.visible} />
    </div>
  );
}

function TwoFactorAdmin(props: { visible: boolean }) {
  const { busy, error, run } = useBusy();
  const [ov, setOv] = useState<MfaOverview | null>(null);
  useEffect(() => { if (props.visible) accessApi.mfaOverview().then(setOv).catch(() => undefined); }, [props.visible]);
  return (
    <Section wide title={t("Two-factor authentication")}
      hint={t("Applies to internal and LDAP accounts. Users who sign in with OAuth2/OIDC or through a reverse proxy get their second factor from that identity provider.")}>
      {ov && (
        <>
          <label className="check" style={{ marginBottom: 10 }}>
            <input type="checkbox" checked={ov.require_all} disabled={busy}
              onChange={(e) => run(async () => setOv(await accessApi.mfaPolicy(e.target.checked)), t("Saved"))} />
            {t("Require two-factor authentication for every internal and LDAP user (they set it up at their next sign-in)")}
          </label>
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("User")}</th><th>{t("Sign-in")}</th><th>{t("Status")}</th><th>{t("Required")}</th><th /></tr></thead>
            <tbody>{ov.users.map((u) => (
              <tr key={u.id}>
                <td>{u.username}</td><td>{u.auth_source}</td>
                <td>{!u.available ? <span className="muted">{t("identity provider")}</span>
                  : u.enabled ? <span className="ok">{t("on")}</span> : <span className="muted">{t("off")}</span>}</td>
                <td>{u.available && (
                  <input type="checkbox" aria-label={t("Require two-factor authentication for {user}", { user: u.username })}
                    checked={u.required || ov.require_all} disabled={busy || ov.require_all}
                    onChange={(e) => run(async () => setOv(await accessApi.mfaRequireUser(u.id, e.target.checked)))} />
                )}</td>
                <td>{u.enabled && (
                  <button className="btn small danger" onClick={() => confirm(t("Reset two-factor authentication of {user}? They can then sign in with their password only (and must set it up again if it is required).", { user: u.username }))
                    && run(async () => setOv(await accessApi.mfaReset(u.id)), t("Two-factor authentication reset"))}>{t("Reset")}</button>
                )}</td>
              </tr>
            ))}</tbody>
          </table></div>
        </>
      )}
      {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
    </Section>
  );
}

function AllTokens(props: { visible: boolean }) {
  const { error, run } = useBusy();
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const load = () => accessApi.allTokens().then(setTokens).catch(() => undefined);
  useEffect(() => { if (props.visible) load(); }, [props.visible]);
  return (
    <Section wide title={t("API tokens of all users")}>
      {tokens.length === 0 ? <div className="muted">{t("No tokens yet.")}</div> : (
        <div className="grid-wrap"><table className="grid">
          <thead><tr><th>{t("User")}</th><th>{t("Name")}</th><th>{t("Scope")}</th><th>{t("Created")}</th><th>{t("Expires")}</th><th>{t("Last used")}</th><th>{t("Status")}</th><th /></tr></thead>
          <tbody>{tokens.map((tk) => (
            <tr key={tk.id}>
              <td>{tk.username}</td><td>{tk.name}</td><td>{tk.scope === "read-only" ? t("read-only") : t("read-write")}</td>
              <td>{fmtTime(tk.created_at)}</td><td>{tk.expires_at ? fmtTime(tk.expires_at) : t("never")}</td>
              <td>{tk.last_used_at ? fmtTime(tk.last_used_at) : "—"}</td><td><TokenStatus status={tk.status} /></td>
              <td>{tk.status === "active" && <button className="btn small danger"
                onClick={() => run(async () => { await accessApi.adminRevokeToken(tk.id); await load(); }, t("Token revoked"))}>{t("Revoke")}</button>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
    </Section>
  );
}

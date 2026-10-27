/**
 * Milestone 1.1 — alerts: email, Slack and webhook channels; rules for events (failed scheduled jobs,
 * failed test restores, validation errors, failed uploads) and conditions checked on a database
 * (OAT gap, long transactions, replication lag, unreachable database); delivery history.
 */
import { useEffect, useState } from "react";
import "./alerts.css";
import { registerMenu, registerTabKind, registerTopbar } from "../../registry";
import { errMsg, useApp, type AppApi } from "../../context";
import { N_, t } from "../../i18n";
import type { Selection } from "../../types";
import { IconActivity, IconPlay, IconPlus, IconRefresh, IconTrash } from "../../components/Icons";
import { Section, useBusy } from "../maintenance/common";
import { fmtTime } from "../maintenance/api";
import { aapi, type AlertItem, type Channel, type ChannelKind, type EventInfo, type Rule } from "./api";

const MASK = "********";

const EVENT_LABELS: Record<string, string> = {
  "schedule.failed": N_("A scheduled job failed"),
  "backup.verify_failed": N_("A backup failed its test restore"),
  "validation.errors": N_("Online validation found errors"),
  "upload.failed": N_("A backup upload to cloud storage failed"),
  "schema.drift": N_("Schema drift found by a scheduled check (and when it is gone)"),
  oat_gap: N_("Transaction gap (next - OAT) above threshold"),
  long_transaction: N_("Transaction running longer than threshold"),
  replication_lag: N_("Replica behind the primary"),
  "database.unreachable": N_("Database unreachable"),
};
// rule states and alert levels, shown through t(state) / t(level)
N_("ok"); N_("firing"); N_("error"); N_("critical"); N_("warning"); N_("info"); N_("resolved");
const UNITS: Record<string, string> = { transactions: N_("transactions"), seconds: N_("seconds"), segments: N_("journal segments") };

const FIELDS: Record<ChannelKind, Array<{ key: string; label: string; secret?: boolean; placeholder?: string; type?: string }>> = {
  email: [
    { key: "host", label: N_("SMTP host"), placeholder: "smtp.example.com" },
    { key: "port", label: N_("Port"), placeholder: "587", type: "number" },
    { key: "security", label: N_("Security") },
    { key: "username", label: N_("User name") },
    { key: "password", label: N_("Password"), secret: true },
    { key: "from_addr", label: N_("From"), placeholder: "fbadmin@example.com" },
    { key: "to", label: N_("To (comma-separated)"), placeholder: "dba@example.com, ops@example.com" },
  ],
  slack: [{ key: "webhook_url", label: N_("Incoming webhook URL"), secret: true, placeholder: "https://hooks.slack.com/services/…" }],
  webhook: [
    { key: "url", label: N_("URL"), secret: true, placeholder: "https://example.com/fbadmin-alerts" },
    { key: "secret", label: N_("Signing secret (optional)"), secret: true },
  ],
};
const KIND_LABEL: Record<ChannelKind, string> = { email: N_("Email (SMTP)"), slack: N_("Slack-compatible webhook"), webhook: N_("Webhook (JSON)") };

interface ChannelForm { id?: string; name: string; kind: ChannelKind; enabled: boolean; settings: Record<string, string> }
interface RuleForm {
  id?: string; name: string; event: string; enabled: boolean; server_id: string; database_id: string; threshold: string;
  peer_server_id: string; peer_database_id: string; channels: string[]; cooldown_minutes: string; notify_resolved: boolean;
}

function channelSummary(c: Channel): string {
  if (c.kind === "email") return `${c.settings.to ?? ""} · ${c.settings.host ?? ""}`;
  if (c.kind === "slack") return c.has_webhook_url ? t("webhook set") : "—";
  return (c.has_url ? t("URL set") : "—") + (c.has_secret ? " · " + t("signed") : "");
}

export function AlertsPanel(props: { visible: boolean; serverId?: string; dbId?: string }) {
  const app = useApp();
  const [events, setEvents] = useState<EventInfo[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [history, setHistory] = useState<AlertItem[]>([]);
  const [cform, setCform] = useState<ChannelForm | null>(null);
  const [rform, setRform] = useState<RuleForm | null>(null);
  const { busy, error, run } = useBusy();

  const load = async () => {
    try {
      const [e, c, r, h] = await Promise.all([aapi.events(), aapi.channels(), aapi.rules(), aapi.history()]);
      setEvents(e); setChannels(c); setRules(r); setHistory(h);
    } catch (e) { app.toast(errMsg(e), "error"); }
  };
  useEffect(() => {
    if (!props.visible) return;
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [props.visible]);

  const ev = (id: string) => events.find((e) => e.id === id);
  const srvOf = (id: string | null) => app.servers.find((s) => s.id === id);
  const dbOf = (sid: string | null, dbid: string | null) => srvOf(sid)?.databases.find((d) => d.id === dbid);
  const scope = (sid: string | null, dbid: string | null) =>
    !sid ? t("all servers") : `${srvOf(sid)?.name ?? sid} / ${dbid ? dbOf(sid, dbid)?.name ?? dbid : t("all databases")}`;

  const newChannel = () => setCform({ name: "", kind: "email", enabled: true, settings: { security: "starttls" } });
  const saveChannel = () => run(async () => {
    if (!cform) return;
    const body = { name: cform.name, kind: cform.kind, enabled: cform.enabled, settings: cform.settings };
    if (cform.id) await aapi.updateChannel(cform.id, body); else await aapi.createChannel(body);
    setCform(null);
    await load();
  }, t("Channel saved"));
  const testChannel = (c: Channel) => run(async () => {
    const r = await aapi.testChannel(c.id);
    if (r.ok) app.toast(t("Test sent via {name}: {result}", { name: c.name, result: r.result ?? "" }));
    else throw new Error(r.error ?? t("failed"));
  });

  const newRule = () => {
    const sid = props.serverId ?? "";
    setRform({ name: "", event: "schedule.failed", enabled: true, server_id: sid, database_id: props.dbId ?? "",
      threshold: "", peer_server_id: "", peer_database_id: "", channels: channels.map((c) => c.id),
      cooldown_minutes: "60", notify_resolved: true });
  };
  useEffect(() => { if (props.dbId && props.visible && !rform && channels.length) newRule(); }, [channels.length]);
  const toRuleForm = (r: Rule): RuleForm => ({
    id: r.id, name: r.name, event: r.event, enabled: r.enabled, server_id: r.server_id ?? "", database_id: r.database_id ?? "",
    threshold: r.threshold === null ? "" : String(r.threshold), peer_server_id: r.peer_server_id ?? "",
    peer_database_id: r.peer_database_id ?? "", channels: r.channels, cooldown_minutes: String(r.cooldown_minutes),
    notify_resolved: r.notify_resolved,
  });
  const saveRule = () => run(async () => {
    if (!rform) return;
    const cond = ev(rform.event)?.kind === "condition";
    const body = {
      name: rform.name || t(EVENT_LABELS[rform.event] ?? rform.event), event: rform.event, enabled: rform.enabled,
      server_id: rform.server_id || null, database_id: rform.server_id ? rform.database_id || null : null,
      threshold: cond && rform.threshold !== "" ? Number(rform.threshold) : null,
      peer_server_id: rform.event === "replication_lag" ? rform.peer_server_id || null : null,
      peer_database_id: rform.event === "replication_lag" ? rform.peer_database_id || null : null,
      channels: rform.channels, cooldown_minutes: Number(rform.cooldown_minutes) || 0, notify_resolved: rform.notify_resolved,
    };
    if (rform.id) await aapi.updateRule(rform.id, body); else await aapi.createRule(body);
    setRform(null);
    await load();
  }, t("Rule saved"));

  const rev = rform ? ev(rform.event) : undefined;
  const isCond = rev?.kind === "condition";
  const rsrv = rform ? srvOf(rform.server_id) : undefined;
  const psrv = rform ? srvOf(rform.peer_server_id) : undefined;

  return (
    <div className="mt-body alerts">
      <Section wide title={t("Alert channels")}
        hint={t("Where alerts are sent. Secrets are stored in the fbadmin data directory (file mode 0600) and never shown again; write env:NAME to read a value from an environment variable instead.")}
        right={<><button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button><button className="btn small primary" onClick={newChannel}><IconPlus size={12} /> {t("New channel")}</button></>}>
        {channels.length === 0 ? <div className="muted">{t("No channels yet.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Kind")}</th><th>{t("Destination")}</th><th /></tr></thead>
            <tbody>{channels.map((c) => (
              <tr key={c.id}>
                <td>{c.enabled ? "" : "⏸ "}{c.name}</td><td>{t(KIND_LABEL[c.kind])}</td><td>{channelSummary(c)}</td>
                <td>
                  <button className="btn small" disabled={busy} onClick={() => testChannel(c)}><IconPlay size={12} /> {t("Send test")}</button>{" "}
                  <button className="btn small" onClick={() => setCform({ id: c.id, name: c.name, kind: c.kind, enabled: c.enabled, settings: { ...c.settings } })}>{t("Edit")}</button>{" "}
                  <button className="btn small danger" aria-label={t("Delete")} onClick={() => run(async () => {
                    if (!confirm(t("Delete channel \"{name}\"?", { name: c.name }))) return;
                    await aapi.deleteChannel(c.id);
                    await load();
                  })}><IconTrash size={12} /></button>
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        {error && <div className="err" style={{ marginTop: 8 }}>{error}</div>}
      </Section>

      {cform && (
        <Section title={cform.id ? t("Edit channel") : t("New channel")}>
          <div className="form mform">
            <label className="l" htmlFor="al-cname">{t("Name")}</label>
            <input id="al-cname" className="text" value={cform.name} onChange={(e) => setCform({ ...cform, name: e.target.value })} />
            <label className="l" htmlFor="al-ckind">{t("Kind")}</label>
            <select id="al-ckind" className="text" value={cform.kind} disabled={!!cform.id}
              onChange={(e) => setCform({ ...cform, kind: e.target.value as ChannelKind, settings: e.target.value === "email" ? { security: "starttls" } : {} })}>
              {(Object.keys(KIND_LABEL) as ChannelKind[]).map((k) => <option key={k} value={k}>{t(KIND_LABEL[k])}</option>)}
            </select>
            {FIELDS[cform.kind].map((f) => <FieldRow key={f.key} f={f} value={cform.settings[f.key] ?? ""}
              onChange={(v) => setCform({ ...cform, settings: { ...cform.settings, [f.key]: v } })} />)}
            <span />
            <label className="check"><input type="checkbox" checked={cform.enabled} onChange={(e) => setCform({ ...cform, enabled: e.target.checked })} /> {t("Enabled")}</label>
          </div>
          {cform.kind === "slack" && <div className="hint">{t("Works with Slack, Mattermost, Rocket.Chat and other services that accept Slack-style incoming webhooks.")}</div>}
          {cform.kind === "webhook" && <div className="hint">{t("fbadmin POSTs the alert as JSON. With a signing secret, the X-Fbadmin-Signature header carries sha256=HMAC-SHA256(secret, body).")}</div>}
          <div className="mt-row" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy || !cform.name} onClick={saveChannel}>{t("Save")}</button>
            <button className="btn" onClick={() => setCform(null)}>{t("Cancel")}</button>
          </div>
        </Section>
      )}

      <Section wide title={t("Alert rules")}
        hint={t("Event rules fire when fbadmin sees the event (a scheduled job, a test restore, a validation, an upload). Condition rules are checked on one database every minute; they alert when the condition starts, repeat after the cool-down while it lasts, and report when it clears.")}
        right={<button className="btn small primary" disabled={!channels.length} title={channels.length ? undefined : t("Add a channel first")} onClick={newRule}><IconPlus size={12} /> {t("New rule")}</button>}>
        {rules.length === 0 ? <div className="muted">{t("No rules yet.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("When")}</th><th>{t("Scope")}</th><th>{t("Threshold")}</th><th>{t("Channels")}</th><th>{t("State")}</th><th /></tr></thead>
            <tbody>{rules.map((r) => {
              const e = ev(r.event);
              return (
                <tr key={r.id}>
                  <td>{r.enabled ? "" : "⏸ "}{r.name}</td>
                  <td>{t(EVENT_LABELS[r.event] ?? r.event)}</td>
                  <td>{scope(r.server_id, r.database_id)}</td>
                  <td className="num">{e?.kind === "condition" && (r.threshold ?? e.threshold) !== null ? `${r.threshold ?? e.threshold} ${e.unit ? t(UNITS[e.unit] ?? e.unit) : ""}` : "—"}</td>
                  <td>{r.channels.map((id) => channels.find((c) => c.id === id)?.name ?? id).join(", ") || <span className="err">{t("none")}</span>}</td>
                  <td style={{ whiteSpace: "normal" }}>{r.status ? (
                    <span title={r.status.error ?? r.status.message ?? ""}>
                      <span className={"pill " + (r.status.state === "ok" ? "finished" : r.status.state === "firing" ? "failed" : "warn")}>{t(r.status.state)}</span>{" "}
                      <span className="muted">{fmtTime(r.status.checked_at)}</span>
                      {r.status.error && <div className="err small">{r.status.error}</div>}
                    </span>
                  ) : e?.kind === "condition" ? <span className="muted">{t("not checked yet")}</span> : "—"}</td>
                  <td>
                    {e?.kind === "condition" && <><button className="btn small" disabled={busy} onClick={() => run(async () => { await aapi.checkRule(r.id); await load(); })}>{t("Check now")}</button>{" "}</>}
                    <button className="btn small" onClick={() => setRform(toRuleForm(r))}>{t("Edit")}</button>{" "}
                    <button className="btn small" onClick={() => run(async () => { await aapi.updateRule(r.id, { enabled: !r.enabled }); await load(); })}>{r.enabled ? t("Disable") : t("Enable")}</button>{" "}
                    <button className="btn small danger" aria-label={t("Delete")} onClick={() => run(async () => {
                      if (!confirm(t("Delete rule \"{name}\"?", { name: r.name }))) return;
                      await aapi.deleteRule(r.id);
                      await load();
                    })}><IconTrash size={12} /></button>
                  </td>
                </tr>
              );
            })}</tbody>
          </table></div>
        )}
      </Section>

      {rform && (
        <Section title={rform.id ? t("Edit rule") : t("New rule")}>
          <div className="form mform">
            <label className="l" htmlFor="al-rname">{t("Name")}</label>
            <input id="al-rname" className="text" value={rform.name} placeholder={t(EVENT_LABELS[rform.event] ?? "")} onChange={(e) => setRform({ ...rform, name: e.target.value })} />
            <label className="l" htmlFor="al-revent">{t("When")}</label>
            <select id="al-revent" className="text" value={rform.event} onChange={(e) => setRform({ ...rform, event: e.target.value, threshold: "" })}>
              <optgroup label={t("Events")}>{events.filter((e) => e.kind === "event").map((e) => <option key={e.id} value={e.id}>{t(EVENT_LABELS[e.id] ?? e.label)}</option>)}</optgroup>
              <optgroup label={t("Conditions (checked every minute)")}>{events.filter((e) => e.kind === "condition").map((e) => <option key={e.id} value={e.id}>{t(EVENT_LABELS[e.id] ?? e.label)}</option>)}</optgroup>
            </select>
            <label className="l" htmlFor="al-rsrv">{isCond ? t("Server / database") : t("Only for")}</label>
            <div className="mt-row">
              <select id="al-rsrv" className="text" value={rform.server_id} onChange={(e) => setRform({ ...rform, server_id: e.target.value, database_id: isCond ? srvOf(e.target.value)?.databases[0]?.id ?? "" : "" })}>
                {!isCond && <option value="">{t("all servers")}</option>}
                {isCond && !rform.server_id && <option value="">{t("choose…")}</option>}
                {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <select className="text" style={{ flex: 1 }} aria-label={t("Database")} value={rform.database_id} disabled={!rform.server_id} onChange={(e) => setRform({ ...rform, database_id: e.target.value })}>
                {!isCond && <option value="">{t("all databases")}</option>}
                {isCond && !rform.database_id && <option value="">{t("choose…")}</option>}
                {rsrv?.databases.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            </div>
            {rform.event === "replication_lag" && <>
              <label className="l" htmlFor="al-rpeer">{t("Other side (replica or primary)")}</label>
              <div className="mt-row">
                <select id="al-rpeer" className="text" value={rform.peer_server_id} onChange={(e) => setRform({ ...rform, peer_server_id: e.target.value, peer_database_id: srvOf(e.target.value)?.databases[0]?.id ?? "" })}>
                  <option value="">{t("choose…")}</option>
                  {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
                <select className="text" style={{ flex: 1 }} aria-label={t("Database")} value={rform.peer_database_id} onChange={(e) => setRform({ ...rform, peer_database_id: e.target.value })}>
                  {psrv?.databases.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
              </div>
            </>}
            {isCond && rev?.threshold !== null && <>
              <label className="l" htmlFor="al-rthr">{t("Threshold")}</label>
              <div className="mt-row">
                <input id="al-rthr" className="text" type="number" min={0} style={{ width: 140 }} value={rform.threshold} placeholder={String(rev?.threshold ?? "")} onChange={(e) => setRform({ ...rform, threshold: e.target.value })} />
                <span className="muted">{rev?.unit ? t(UNITS[rev.unit] ?? rev.unit) : ""}</span>
              </div>
            </>}
            <span className="l">{t("Send to")}</span>
            <div className="al-channels">
              {channels.map((c) => (
                <label key={c.id} className="check"><input type="checkbox" checked={rform.channels.includes(c.id)}
                  onChange={(e) => setRform({ ...rform, channels: e.target.checked ? [...rform.channels, c.id] : rform.channels.filter((x) => x !== c.id) })} /> {c.name}</label>
              ))}
            </div>
            <label className="l" htmlFor="al-rcool">{t("Repeat at most every (minutes)")}</label>
            <input id="al-rcool" className="text" type="number" min={0} style={{ width: 140 }} value={rform.cooldown_minutes} onChange={(e) => setRform({ ...rform, cooldown_minutes: e.target.value })} />
            <span />
            <div>
              {isCond && <label className="check"><input type="checkbox" checked={rform.notify_resolved} onChange={(e) => setRform({ ...rform, notify_resolved: e.target.checked })} /> {t("Send a message when the condition clears")}</label>}
              <label className="check"><input type="checkbox" checked={rform.enabled} onChange={(e) => setRform({ ...rform, enabled: e.target.checked })} /> {t("Enabled")}</label>
            </div>
          </div>
          <div className="mt-row" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy || !rform.channels.length || (isCond && !rform.database_id) || (rform.event === "replication_lag" && !rform.peer_database_id)} onClick={saveRule}>{t("Save")}</button>
            <button className="btn" onClick={() => setRform(null)}>{t("Cancel")}</button>
          </div>
        </Section>
      )}

      <Section wide title={t("Sent alerts")} right={history.length > 0 && <button className="btn small" onClick={() => run(async () => { if (confirm(t("Clear the alert history?"))) { await aapi.clearHistory(); await load(); } })}>{t("Clear")}</button>}>
        {history.length === 0 ? <div className="muted">{t("No alerts sent yet.")}</div> : (
          <div className="grid-wrap" style={{ maxHeight: 360 }}><table className="grid">
            <thead><tr><th>{t("Time")}</th><th>{t("Level")}</th><th>{t("Alert")}</th><th>{t("Database")}</th><th>{t("Delivery")}</th></tr></thead>
            <tbody>{history.map((a) => (
              <tr key={a.id}>
                <td>{fmtTime(a.time)}</td>
                <td><span className={"pill al-" + a.level}>{t(a.level)}</span></td>
                <td style={{ whiteSpace: "normal" }}><b>{a.title}</b><div className="muted small">{a.message}</div></td>
                <td>{[a.server, a.database].filter(Boolean).join(" / ") || "—"}</td>
                <td style={{ whiteSpace: "normal" }}>{a.deliveries.length === 0 ? <span className="muted">{t("pending")}</span> : a.deliveries.map((d) => (
                  <div key={d.channel_id} className={d.ok ? "ok" : "err"} title={d.error ?? d.result}>{d.ok ? "✓" : "✗"} {d.channel}{d.error ? `: ${d.error}` : ""}</div>
                ))}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Section>
    </div>
  );
}

function FieldRow(props: { f: { key: string; label: string; secret?: boolean; placeholder?: string; type?: string }; value: string; onChange: (v: string) => void }) {
  const { f, value } = props;
  const id = `al-f-${f.key}`;
  return <>
    <label className="l" htmlFor={id}>{t(f.label)}</label>
    {f.key === "security" ? (
      <select id={id} className="text" value={value || "starttls"} onChange={(e) => props.onChange(e.target.value)}>
        <option value="starttls">STARTTLS</option><option value="ssl">SSL/TLS</option><option value="none">{t("none (plain SMTP)")}</option>
      </select>
    ) : (
      <input id={id} className="text" type={f.secret && value !== MASK ? "password" : f.type ?? "text"} value={value} placeholder={f.placeholder}
        autoComplete="off" onFocus={(e) => { if (f.secret && value === MASK) e.target.select(); }} onChange={(e) => props.onChange(e.target.value)} />
    )}
  </>;
}

registerTabKind("alerts", {
  icon: <IconActivity size={13} />,
  render: (tab, visible) => <AlertsPanel visible={visible} serverId={tab.props?.serverId} dbId={tab.props?.dbId} />,
  same: () => true,
});

registerTopbar({
  id: "alerts",
  render: (app) => <button className="tb" onClick={() => app.openTab({ kind: "alerts", title: t("Alerts") })}>🔔 {t("Alerts")}</button>,
});

registerMenu((app: AppApi, sel: Selection) => {
  if (sel.kind !== "database" || !sel.serverId || !sel.dbId) return [];
  return [{ label: t("Alert rules…"), icon: <IconActivity />, onClick: () => app.openTab({ kind: "alerts", title: t("Alerts"), props: { serverId: sel.serverId, dbId: sel.dbId } }) }];
});

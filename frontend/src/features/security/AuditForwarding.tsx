/** Roadmap 2.2: audit log forwarding to syslog and webhooks (administrators). */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { fmtTime } from "../maintenance/api";
import "./forwarding.css";

export interface Destination {
  id: string; name: string; kind: "syslog" | "webhook"; enabled: boolean; source: "stored" | "environment";
  host: string | null; port: number | null; protocol: "udp" | "tcp" | "tls"; facility: string; tls_verify: boolean; ca_file: string | null;
  url: string | null; secret: string | null; token: string | null; has_secret: boolean; has_token: boolean;
  categories: string[]; failures_only: boolean;
  cursor: number | null; backlog: number | null; last_ok_at: number | null; last_error: string | null; last_error_at: number | null;
  sent: number; last_result: string | null;
}
const base = "/api/admin/audit/forwarding";
const fapi = {
  list: () => request<{ enabled: boolean; newest: number; destinations: Destination[] }>("GET", base),
  create: (d: Record<string, unknown>) => request<Destination>("POST", base, d),
  update: (id: string, d: Record<string, unknown>) => request<Destination>("PUT", `${base}/${id}`, d),
  remove: (id: string) => request<void>("DELETE", `${base}/${id}`),
  test: (id: string) => request<{ ok: boolean; result?: string; error?: string }>("POST", `${base}/${id}/test`),
  resend: (id: string, after_id: number) => request<Destination>("POST", `${base}/${id}/resend`, { after_id }),
};

const FACILITIES = ["local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7", "auth", "authpriv", "user"];
const CATEGORIES = ["DDL", "DML", "TX", "ACTION", "SECURITY", "OTHER"];

interface Form {
  id?: string; name: string; kind: "syslog" | "webhook"; host: string; port: string; protocol: "udp" | "tcp" | "tls"; facility: string;
  tls_verify: boolean; ca_file: string; url: string; secret: string; token: string; categories: string[]; failures_only: boolean; start: "now" | "all";
}
const emptyForm = (): Form => ({ name: "", kind: "syslog", host: "", port: "", protocol: "udp", facility: "local0", tls_verify: true, ca_file: "",
  url: "", secret: "", token: "", categories: [], failures_only: false, start: "now" });
const toForm = (d: Destination): Form => ({ id: d.id, name: d.name, kind: d.kind, host: d.host ?? "", port: d.port ? String(d.port) : "", protocol: d.protocol,
  facility: d.facility, tls_verify: d.tls_verify, ca_file: d.ca_file ?? "", url: d.url ?? "", secret: d.secret ?? "", token: d.token ?? "",
  categories: d.categories, failures_only: d.failures_only, start: "now" });

function payload(f: Form): Record<string, unknown> {
  const v = (s: string) => (s.trim() ? s.trim() : null);
  const body: Record<string, unknown> = { name: f.name.trim(), categories: f.categories, failures_only: f.failures_only };
  if (f.kind === "syslog") Object.assign(body, { host: v(f.host), port: f.port.trim() ? Number(f.port) : null, protocol: f.protocol, facility: f.facility, tls_verify: f.tls_verify, ca_file: v(f.ca_file) });
  else Object.assign(body, { url: v(f.url), secret: v(f.secret), token: v(f.token) });
  if (!f.id) Object.assign(body, { kind: f.kind, start: f.start });
  return body;
}

function DestinationForm(props: { value: Form; onSave: (f: Form) => void; onCancel: () => void; busy: boolean }) {
  const [f, setF] = useState(props.value);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF({ ...f, [k]: v });
  const syslog = f.kind === "syslog";
  return (
    <div className="form mform fwd-form">
      <label className="l" htmlFor="fwd-name">{t("Name")}</label>
      <input id="fwd-name" className="text" value={f.name} onChange={(e) => set("name", e.target.value)} />
      {!f.id && <>
        <label className="l" htmlFor="fwd-kind">{t("Send to")}</label>
        <select id="fwd-kind" className="text" value={f.kind} onChange={(e) => set("kind", e.target.value as Form["kind"])}>
          <option value="syslog">{t("syslog (RFC 5424)")}</option><option value="webhook">{t("a webhook (JSON over HTTPS)")}</option>
        </select>
      </>}
      {syslog ? <>
        <label className="l" htmlFor="fwd-host">{t("Host")}</label>
        <input id="fwd-host" className="text mono" placeholder="logs.example.com" value={f.host} onChange={(e) => set("host", e.target.value)} />
        <label className="l" htmlFor="fwd-proto">{t("Transport")}</label>
        <div className="mt-row">
          <select id="fwd-proto" className="text" value={f.protocol} onChange={(e) => set("protocol", e.target.value as Form["protocol"])}>
            <option value="udp">UDP</option><option value="tcp">TCP</option><option value="tls">TLS</option>
          </select>
          <label htmlFor="fwd-port" className="muted">{t("Port")}</label>
          <input id="fwd-port" className="text" type="number" min={1} max={65535} style={{ width: 90 }} placeholder={f.protocol === "tls" ? "6514" : "514"} value={f.port} onChange={(e) => set("port", e.target.value)} />
        </div>
        <label className="l" htmlFor="fwd-fac">{t("Facility")}</label>
        <select id="fwd-fac" className="text" value={f.facility} onChange={(e) => set("facility", e.target.value)}>
          {FACILITIES.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
        {f.protocol === "tls" && <>
          <label className="l" htmlFor="fwd-ca">{t("CA certificate (optional)")}</label>
          <input id="fwd-ca" className="text mono" placeholder="/etc/ssl/certs/logs-ca.pem" value={f.ca_file} onChange={(e) => set("ca_file", e.target.value)} />
          <span />
          <label className="check"><input type="checkbox" checked={f.tls_verify} onChange={(e) => set("tls_verify", e.target.checked)} /> {t("Verify the server's certificate")}</label>
        </>}
      </> : <>
        <label className="l" htmlFor="fwd-url">{t("URL")}</label>
        <input id="fwd-url" className="text mono" placeholder="https://siem.example.com/fbadmin" value={f.url} onChange={(e) => set("url", e.target.value)} />
        <label className="l" htmlFor="fwd-secret">{t("Signing secret (optional)")}</label>
        <input id="fwd-secret" className="text mono" type="password" autoComplete="off" placeholder={t("HMAC key, or env:NAME")} value={f.secret} onChange={(e) => set("secret", e.target.value)} />
        <label className="l" htmlFor="fwd-token">{t("Bearer token (optional)")}</label>
        <input id="fwd-token" className="text mono" type="password" autoComplete="off" placeholder={t("or env:NAME")} value={f.token} onChange={(e) => set("token", e.target.value)} />
      </>}
      <span className="l">{t("Entries")}</span>
      <div>
        <div className="fwd-cats" role="group" aria-label={t("Kinds of entries")}>
          {CATEGORIES.map((c) => (
            <label key={c} className="check"><input type="checkbox" checked={f.categories.includes(c)}
              onChange={(e) => set("categories", e.target.checked ? [...f.categories, c] : f.categories.filter((x) => x !== c))} /> {c}</label>
          ))}
        </div>
        <div className="muted">{t("None ticked: every kind.")}</div>
        <label className="check"><input type="checkbox" checked={f.failures_only} onChange={(e) => set("failures_only", e.target.checked)} /> {t("Only failed statements")}</label>
      </div>
      {!f.id && <>
        <span className="l">{t("Start with")}</span>
        <div className="fwd-radios" role="radiogroup" aria-label={t("Start with")}>
          <label className="check"><input type="radio" name="fwd-start" checked={f.start === "now"} onChange={() => set("start", "now")} /> {t("new entries")}</label>
          <label className="check"><input type="radio" name="fwd-start" checked={f.start === "all"} onChange={() => set("start", "all")} /> {t("the whole audit log")}</label>
        </div>
      </>}
      <span />
      <div className="mt-row">
        <button className="btn primary" disabled={props.busy || !f.name.trim() || (syslog ? !f.host.trim() : !f.url.trim())} onClick={() => props.onSave(f)}>{t("Save")}</button>
        <button className="btn" onClick={props.onCancel}>{t("Cancel")}</button>
      </div>
    </div>
  );
}

export function AuditForwarding(props: { visible: boolean }) {
  const app = useApp();
  const [items, setItems] = useState<Destination[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [form, setForm] = useState<Form | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = () => fapi.list().then((r) => { setItems(r.destinations); setEnabled(r.enabled); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => {
    if (!props.visible) return;
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [props.visible]);
  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    try { await fn(); if (ok) app.toast(ok, "ok"); await load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <div className="fwd pad">
      <h3>{t("Forwarding")}</h3>
      <p className="muted">{t("Every audit entry is also sent to these destinations, in order. A destination that is down gets the entries later: nothing is lost while it is down or while fbadmin is stopped.")}</p>
      {!enabled && <div className="warn">{t("Auditing is off: nothing is forwarded.")}</div>}
      {error && <div className="err">{error}</div>}
      {items.length > 0 && <div className="grid-wrap"><table className="grid">
        <thead><tr><th>{t("Name")}</th><th>{t("Destination")}</th><th>{t("Sent")}</th><th>{t("Waiting")}</th><th>{t("Last result")}</th><th /></tr></thead>
        <tbody>{items.map((d) => (
          <tr key={d.id}>
            <td>{d.enabled ? "" : "⏸ "}{d.name}{d.source === "environment" && <div className="muted small">{t("from the environment")}</div>}</td>
            <td className="mono">{d.kind === "syslog" ? `${d.protocol}://${d.host}${d.port ? ":" + d.port : ""} · ${d.facility}` : d.url}</td>
            <td className="num">{d.sent}</td>
            <td className={"num" + (d.backlog ? " warn" : "")}>{d.backlog ?? "—"}</td>
            <td style={{ whiteSpace: "normal" }}>{d.last_error
              ? <span className="err">{fmtTime(d.last_error_at)}: {d.last_error}</span>
              : d.last_ok_at ? <span>{fmtTime(d.last_ok_at)}: {d.last_result}</span> : "—"}</td>
            <td className="fwd-btns">
              <button className="btn small" onClick={() => act(async () => {
                const r = await fapi.test(d.id);
                app.toast(r.ok ? t("Test message sent: {result}", { result: r.result ?? "" }) : t("Test failed: {error}", { error: r.error ?? "" }), r.ok ? "ok" : "error");
              })}>{t("Test")}</button>{" "}
              {d.source === "stored" && <>
                <button className="btn small" onClick={() => setForm(toForm(d))}>{t("Edit")}</button>{" "}
                <button className="btn small" onClick={() => act(() => fapi.update(d.id, { enabled: !d.enabled }))}>{d.enabled ? t("Pause") : t("Resume")}</button>{" "}
              </>}
              <button className="btn small" onClick={() => {
                const v = prompt(t("Send the entries after this entry number again (0: the whole log):"), "0");
                if (v !== null && /^\d+$/.test(v.trim())) act(() => fapi.resend(d.id, Number(v.trim())));
              }}>{t("Resend…")}</button>{" "}
              {d.source === "stored" && <button className="btn small danger" aria-label={t("Delete {name}", { name: d.name })} title={t("Delete")}
                onClick={() => confirm(t("Delete the destination {name}?", { name: d.name })) && act(() => fapi.remove(d.id))}>✕</button>}
            </td>
          </tr>
        ))}</tbody>
      </table></div>}
      {form ? <DestinationForm value={form} busy={busy} onCancel={() => setForm(null)} onSave={async (f) => {
        setBusy(true);
        try {
          if (f.id) await fapi.update(f.id, payload(f)); else await fapi.create(payload(f));
          setForm(null); await load(); app.toast(t("Saved"), "ok");
        } catch (e) { app.toast(errMsg(e), "error"); } finally { setBusy(false); }
      }} /> : <button className="btn small primary" onClick={() => setForm(emptyForm())}>{t("Add a destination")}</button>}
    </div>
  );
}

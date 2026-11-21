/** Roadmap 2.5: maintenance windows - alerts muted while one is open; approved change requests applied in the next. */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp, type AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconPlus, IconRefresh } from "../../components/Icons";
import { Check, Section } from "../maintenance/common";
import { fmtTime } from "../maintenance/api";
import "../maintenance/maintenance.css";

export interface MWindow {
  id: string; name: string; server_id: string | null; database_id: string | null; kind: "recurring" | "once";
  cron: string | null; duration_minutes: number; start: number | null; end: number | null; mute_alerts: boolean; enabled: boolean;
  created_by: string | null; open: boolean; open_until: number | null; next_start: number | null; next_end: number | null;
}
type Body = Omit<MWindow, "id" | "created_by" | "open" | "open_until" | "next_start" | "next_end">;
const api = {
  list: (sid: string, dbid: string) => request<MWindow[]>("GET", `/api/maintenance-windows?server_id=${encodeURIComponent(sid)}&database_id=${encodeURIComponent(dbid)}`),
  create: (b: Body) => request<MWindow>("POST", "/api/maintenance-windows", b),
  update: (id: string, b: Body) => request<MWindow>("PUT", `/api/maintenance-windows/${encodeURIComponent(id)}`, b),
  remove: (id: string) => request<void>("DELETE", `/api/maintenance-windows/${encodeURIComponent(id)}`),
};
const local = (ts: number) => { const d = new Date(ts * 1000); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
const fromLocal = (s: string) => new Date(s).getTime() / 1000;

function WindowsTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [items, setItems] = useState<MWindow[]>([]);
  const [form, setForm] = useState<(Body & { scope: "db" | "server" | "all"; from: string; to: string }) | null>(null);
  const load = () => api.list(sid, dbid).then(setItems).catch((e) => app.toast(errMsg(e), "error"));
  useEffect(() => { if (props.visible) load(); }, [props.visible]);
  const scopeLabel = (w: MWindow) => (!w.server_id ? t("every database") : !w.database_id ? t("every database of the server") : t("this database"));
  const now = Date.now() / 1000;
  const save = async () => {
    if (!form) return;
    const { scope, from, to, ...b } = form;
    const body: Body = { ...b, server_id: scope === "all" ? null : sid, database_id: scope === "db" ? dbid : null,
      start: b.kind === "once" ? fromLocal(from) : null, end: b.kind === "once" ? fromLocal(to) : null, cron: b.kind === "recurring" ? b.cron : null };
    try { await api.create(body); app.toast(t("Saved"), "ok"); setForm(null); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <div className="mt-body">
      <Section wide title={t("Maintenance windows")}
        hint={t("While a window is open, alerts for what it covers are muted (recorded in the alert history, not sent). An approved change request can wait for the next window, and fbadmin applies it when the window opens.")}
        right={<>
          <button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>
          <button className="btn primary small" onClick={() => setForm({ name: t("Nightly maintenance"), scope: "db", kind: "recurring", cron: "0 22 * * *", duration_minutes: 120,
            start: null, end: null, mute_alerts: true, enabled: true, server_id: sid, database_id: dbid, from: local(now + 3600), to: local(now + 3 * 3600) })}><IconPlus size={12} /> {t("New window")}</button>
        </>}>
        {items.length === 0 ? <div className="muted">{t("No maintenance window covers this database.")}</div> : (
          <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Maintenance windows")}><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Covers")}</th><th>{t("When")}</th><th>{t("Next")}</th><th>{t("Alerts")}</th><th /></tr></thead>
            <tbody>{items.map((w) => (
              <tr key={w.id}>
                <td>{w.enabled ? "" : "⏸ "}{w.name}{w.open && <> <span className="pill running">{t("open")}</span></>}</td>
                <td>{scopeLabel(w)}</td>
                <td className="mono-cell">{w.kind === "recurring" ? t("{cron}, {n} min", { cron: w.cron ?? "", n: w.duration_minutes }) : `${fmtTime(w.start)} – ${fmtTime(w.end)}`}</td>
                <td>{w.open ? t("until {time}", { time: fmtTime(w.open_until) }) : w.next_start ? fmtTime(w.next_start) : "–"}</td>
                <td>{w.mute_alerts ? t("muted") : t("sent")}</td>
                <td>
                  <button className="btn small" onClick={async () => { try { await api.update(w.id, { ...w, enabled: !w.enabled }); load(); } catch (e) { app.toast(errMsg(e), "error"); } }}>{w.enabled ? t("Pause") : t("Resume")}</button>{" "}
                  <button className="btn small danger" onClick={async () => { if (!window.confirm(t("Delete the window {name}?", { name: w.name }))) return; try { await api.remove(w.id); load(); } catch (e) { app.toast(errMsg(e), "error"); } }}>{t("Delete")}</button>
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Section>
      {form && <Section wide title={t("New maintenance window")}>
        <div className="form mform">
          <label className="l" htmlFor="mw-name">{t("Name")}</label>
          <input id="mw-name" className="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <label className="l" htmlFor="mw-scope">{t("Covers")}</label>
          <select id="mw-scope" className="text" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value as "db" | "server" | "all" })}>
            <option value="db">{t("this database")}</option><option value="server">{t("every database of the server")}</option><option value="all">{t("every database")}</option>
          </select>
          <label className="l" htmlFor="mw-kind">{t("Kind")}</label>
          <select id="mw-kind" className="text" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as "recurring" | "once" })}>
            <option value="recurring">{t("recurring")}</option><option value="once">{t("once")}</option>
          </select>
          {form.kind === "recurring" ? <>
            <label className="l" htmlFor="mw-cron">{t("Opens (cron)")}</label>
            <input id="mw-cron" className="text mono" value={form.cron ?? ""} onChange={(e) => setForm({ ...form, cron: e.target.value })} />
            <label className="l" htmlFor="mw-dur">{t("Lasts (minutes)")}</label>
            <input id="mw-dur" className="text" type="number" min={5} value={form.duration_minutes} onChange={(e) => setForm({ ...form, duration_minutes: Number(e.target.value) || 120 })} />
          </> : <>
            <label className="l" htmlFor="mw-from">{t("From")}</label>
            <input id="mw-from" className="text" type="datetime-local" value={form.from} onChange={(e) => setForm({ ...form, from: e.target.value })} />
            <label className="l" htmlFor="mw-to">{t("To")}</label>
            <input id="mw-to" className="text" type="datetime-local" value={form.to} onChange={(e) => setForm({ ...form, to: e.target.value })} />
          </>}
          <span className="l" />
          <Check label={t("Mute alerts while it is open")} checked={form.mute_alerts} onChange={(mute_alerts) => setForm({ ...form, mute_alerts })} />
        </div>
        <div className="mt-row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={!form.name.trim()} onClick={save}>{t("Save")}</button>
          <button className="btn" onClick={() => setForm(null)}>{t("Cancel")}</button>
        </div>
      </Section>}
    </div>
  );
}

export function openWindows(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "maintenance-windows", title: t("{db} · maintenance windows", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("maintenance-windows", {
  icon: <span aria-hidden="true" style={{ fontSize: 12 }}>◷</span>,
  render: (tab, visible) => <WindowsTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Maintenance windows…"), onClick: () => openWindows(app, sid, dbid) }];
});

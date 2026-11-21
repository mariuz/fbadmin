/** Roadmap 2.5: fleet health - every database with its newest check-up, forecast, backup, change requests, drift
 * and trace state, from what fbadmin already keeps (no database is connected to). */
import { useEffect, useMemo, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp, type AppApi } from "../../context";
import { N_, t } from "../../i18n";
import { registerTabKind, registerTopbar } from "../../registry";
import { IconRefresh } from "../../components/Icons";
import { fmtAge } from "../monitoring/api";
import { openPerformance } from "../monitoring";
import { openCheckup } from "../checkup";
import { openForecast } from "../forecast";
import { openChanges } from "../changes";
import "./fleet.css";

type Status = "critical" | "warning" | "ok" | "unknown";
export interface FleetRow {
  server_id: string; server: string; database_id: string; database: string; status: Status; problems: string[];
  history: { tracked: boolean; last_sample: number | null; error: string | null };
  checkup: { at: number; critical: number; warning: number; info: number; source: string } | null;
  forecast: { at: number; severity: "ok" | "warning" | "critical"; title: string | null; findings: number; enough: boolean } | null;
  backup: { at: number | null; age_hours: number | null; file: string | null; last_failed_at: number | null; error: string | null } | null;
  drift: { at: number; drift: boolean; failed: boolean } | null;
  changes: { open: number; approved: number; failed: number };
  trace: { enabled: boolean; state: string; error: string | null; captured: number };
  alerts_24h: { total: number; critical: number; last: string | null };
  schedules: string[];
}
export interface Fleet { generated_at: number; databases: FleetRow[]; counts: Record<Status, number> }
export const fleetApi = { get: () => request<Fleet>("GET", "/api/fleet") };

// continuous trace states, shown through t(state)
N_("waiting"); N_("stopped");
const RANK: Record<Status, number> = { critical: 3, warning: 2, ok: 1, unknown: 0 };
const STATUS_LABEL: Record<Status, string> = { critical: N_("Critical"), warning: N_("Warning"), ok: N_("OK"), unknown: N_("Unknown") };
const ago = (ts: number | null | undefined, now: number) => (ts ? t("{age} ago", { age: fmtAge(now - ts) }) : "–");
type SortKey = "status" | "database" | "checkup" | "backup" | "alerts";

function Pill(props: { s: Status | "ok" }) {
  const cls = { critical: "failed", warning: "running", ok: "finished", unknown: "cancelled" }[props.s];
  return <span className={"pill " + cls}>{t(STATUS_LABEL[props.s])}</span>;
}

function FleetTab(props: { visible: boolean }) {
  const app = useApp();
  const [data, setData] = useState<Fleet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"" | "attention" | Status>("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("status");
  const load = () => fleetApi.get().then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => {
    if (!props.visible) return;
    load();
    const h = setInterval(load, 60000);
    return () => clearInterval(h);
  }, [props.visible]);
  const now = Date.now() / 1000;
  const rows = useMemo(() => {
    let r = data?.databases ?? [];
    if (filter === "attention") r = r.filter((x) => x.status === "critical" || x.status === "warning");
    else if (filter) r = r.filter((x) => x.status === filter);
    const ql = q.trim().toLowerCase();
    if (ql) r = r.filter((x) => `${x.server} ${x.database} ${x.problems.join(" ")}`.toLowerCase().includes(ql));
    const key: Record<SortKey, (x: FleetRow) => number | string> = {
      status: (x) => -RANK[x.status], database: (x) => `${x.server}/${x.database}`.toLowerCase(),
      checkup: (x) => -((x.checkup?.critical ?? 0) * 1000 + (x.checkup?.warning ?? 0)),
      backup: (x) => -(x.backup?.age_hours ?? (x.backup ? 0 : 1e9)), alerts: (x) => -x.alerts_24h.total,
    };
    return [...r].sort((a, b) => { const ka = key[sort](a), kb = key[sort](b); return ka < kb ? -1 : ka > kb ? 1 : a.database.localeCompare(b.database); });
  }, [data, filter, q, sort]);
  const th = (k: SortKey, label: string) => (
    <th aria-sort={sort === k ? "descending" : "none"}><button className="fl-sort" onClick={() => setSort(k)}>{label}{sort === k ? " ▾" : ""}</button></th>
  );
  return (
    <div className="fl">
      <div className="toolbar fl-bar">
        <strong>{t("Fleet health")}</strong>
        {data && (["critical", "warning", "ok", "unknown"] as Status[]).map((s) => data.counts[s] ? <span key={s}><Pill s={s} /> {data.counts[s]}</span> : null)}
        <span className="sep" />
        <select className="text" aria-label={t("Show")} value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
          <option value="">{t("all databases")}</option>
          <option value="attention">{t("needing attention")}</option>
          <option value="critical">{t("critical")}</option><option value="warning">{t("warning")}</option>
          <option value="ok">{t("ok")}</option><option value="unknown">{t("nothing recorded")}</option>
        </select>
        <input className="text" type="search" aria-label={t("Search")} placeholder={t("Search…")} value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>
        <button className="btn small" onClick={() => app.openTab({ kind: "overview", title: t("Overview") })}>{t("Live overview")}</button>
      </div>
      <p className="muted fl-hint">{t("From what fbadmin records: the history sampler, check-ups, the capacity forecast, scheduled backups and drift checks, change requests, the continuous trace and alerts. No database is connected to; the live overview checks reachability and sizes.")}</p>
      {error && <div className="err">{error}</div>}
      {data && rows.length === 0 && <div className="muted">{data.databases.length ? t("No database matches.") : t("No databases registered.")}</div>}
      {rows.length > 0 && <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Fleet health")}><table className="grid fl-grid">
        <thead><tr>{th("status", t("Status"))}{th("database", t("Database"))}{th("checkup", t("Check-up"))}<th>{t("Forecast")}</th>{th("backup", t("Backup"))}<th>{t("Drift")}</th><th>{t("Change requests")}</th><th>{t("History / trace")}</th>{th("alerts", t("Alerts (24 h)"))}</tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={`${r.server_id}/${r.database_id}`} className={"fl-" + r.status}>
            <td><Pill s={r.status} />{r.problems.length > 0 && <ul className="fl-problems">{r.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}</td>
            <td><strong>{r.database}</strong><div className="muted">{r.server}</div></td>
            <td>{r.checkup ? <button className="linkish" onClick={() => openCheckup(app, r.server_id, r.database_id)}>
              {r.checkup.critical ? t("{n} critical", { n: r.checkup.critical }) : r.checkup.warning ? t("{n} warning(s)", { n: r.checkup.warning }) : t("no problems")}
              <div className="muted">{ago(r.checkup.at, now)}</div></button> : <button className="linkish muted" onClick={() => openCheckup(app, r.server_id, r.database_id)}>{t("never run")}</button>}</td>
            <td>{r.forecast ? <button className="linkish" onClick={() => openForecast(app, r.server_id, r.database_id)} title={r.forecast.title ?? undefined}>
              {r.forecast.severity === "ok" ? (r.forecast.enough ? t("no limit near") : t("collecting")) : <Pill s={r.forecast.severity} />}
              <div className="muted fl-clip">{r.forecast.title ?? ago(r.forecast.at, now)}</div></button>
              : <button className="linkish muted" onClick={() => openForecast(app, r.server_id, r.database_id)}>–</button>}</td>
            <td>{r.backup ? <>{r.backup.error ? <span className="err">{t("failed")}</span> : null}
              <div>{r.backup.at ? ago(r.backup.at, now) : t("none succeeded")}</div>{r.backup.error && <div className="muted fl-clip" title={r.backup.error}>{r.backup.error}</div>}</>
              : <span className="muted">{t("not scheduled")}</span>}</td>
            <td>{r.drift ? (r.drift.drift ? <span className="pill drift">{t("drift")}</span> : r.drift.failed ? <span className="err">{t("failed")}</span> : t("no drift")) : <span className="muted">–</span>}</td>
            <td>{r.changes.open + r.changes.approved + r.changes.failed > 0
              ? <button className="linkish" onClick={() => openChanges(app, r.server_id, r.database_id)}>{t("{open} open, {approved} approved", { open: r.changes.open, approved: r.changes.approved })}{r.changes.failed ? <div className="err">{t("{n} failed", { n: r.changes.failed })}</div> : null}</button>
              : <span className="muted">–</span>}</td>
            <td><button className="linkish" onClick={() => openPerformance(app, r.server_id, r.database_id, "history")}>
              {r.history.tracked ? t("sampled {age}", { age: ago(r.history.last_sample, now) }) : <span className="muted">{t("not recorded")}</span>}
              {r.trace.enabled && <div className={r.trace.state === "running" ? "muted" : "err"}>{t("trace: {state}", { state: t(r.trace.state) })}</div>}</button></td>
            <td className="num">{r.alerts_24h.total ? <span title={r.alerts_24h.last ?? undefined}>{r.alerts_24h.total}{r.alerts_24h.critical ? ` (${t("{n} critical", { n: r.alerts_24h.critical })})` : ""}</span> : "0"}</td>
          </tr>
        ))}</tbody>
      </table></div>}
    </div>
  );
}

export function openFleet(app: AppApi) {
  app.openTab({ kind: "fleet", title: t("Fleet health") });
}

registerTabKind("fleet", {
  icon: <span aria-hidden="true" style={{ fontSize: 12 }}>▤</span>,
  render: (_tab, visible) => <FleetTab visible={visible} />,
  same: () => true,
});

registerTopbar({
  id: "fleet",
  render: (app) => <button className="tb" onClick={() => openFleet(app)}>▤ {t("Fleet")}</button>,
});

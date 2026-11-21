/** Roadmap 2.5: unused index finder - indices that appear in no recorded plan for N days, as candidates to drop. */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp, type AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { N_, t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconRefresh } from "../../components/Icons";
import { Section } from "../maintenance/common";
import { openPerformance } from "../monitoring";
import { openChanges } from "../changes";
import "../maintenance/maintenance.css";
import "./unused-indices.css";

type Status = "unused" | "used" | "inactive" | "constraint";
interface Row {
  schema: string | null; name: string; table: string; columns: string[]; unique: boolean; expression: boolean;
  status: Status; constraint: string | null; last_used: number | null; table_writes: number | null;
  nodes: number | null; depth: number | null; drop_sql: string | null;
}
interface Report {
  days: number; quality: "none" | "little" | "partial" | "good"; writes_known: boolean; gstat_at: number | null;
  evidence: { plans: number; runs: number; sources: Record<string, number>; evidence_days: number; trace: boolean; tracked: boolean };
  counts: Record<Status, number>; indices: Row[]; script: string;
}
const get = (sid: string, dbid: string, days: number) =>
  request<Report>("GET", `/api/servers/${encodeURIComponent(sid)}/databases/${encodeURIComponent(dbid)}/monitoring/unused-indices?days=${days}`);

const STATUS: Record<Status, [string, string]> = {
  unused: [N_("unused"), "failed"], used: [N_("used"), "finished"], inactive: [N_("inactive"), "cancelled"],
  constraint: [N_("enforces a constraint"), "cancelled"],
};
const QUALITY: Record<Report["quality"], string> = {
  none: N_("History isn't recorded for this database, so no plan was seen: record it, and turn on the continuous trace."),
  little: N_("Little evidence: few plans or a short period. Fast statements are seen only by the continuous trace; turn it on with a low threshold and come back after a typical week."),
  partial: N_("Partial evidence: without the continuous trace, only statements slow enough to be sampled were seen. An index used only by fast statements shows as unused."),
  good: N_("Good evidence: the continuous trace saw the statements of the whole period. Still check reports and jobs that run rarely (month end) before dropping."),
};
const fmt = (n: number | null) => (n == null ? "–" : n.toLocaleString());

function UnusedTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [days, setDays] = useState(30);
  const [r, setR] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const load = () => get(sid, dbid, days).then((x) => { setR(x); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { if (props.visible) load(); }, [props.visible, days]);
  const rows = (r?.indices ?? []).filter((x) => all || x.status === "unused");
  return (
    <div className="mt-body ui">
      <Section wide title={t("Unused indices")}
        hint={t("Indices that appear in no plan fbadmin recorded (continuous trace, history sampler, slow Query Tool statements) in the period. Every insert, update and delete of their table still maintains them.")}
        right={<>
          <select className="text" aria-label={t("Period")} value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[7, 30, 90, 180].map((d) => <option key={d} value={d}>{t("last {n} days", { n: d })}</option>)}
          </select>
          <button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>
        </>}>
        {error && <div className="err">{error}</div>}
        {r && <>
          <div className={"alerts"}><div className={"alert " + (r.quality === "good" ? "good" : "warning")}>
            <span className="lvl">{t("evidence")}</span>
            <span>{t(QUALITY[r.quality])} {t("{plans} plan(s) from {runs} run(s) over {days} day(s).", { plans: r.evidence.plans, runs: r.evidence.runs, days: r.evidence.evidence_days })}{" "}
              {!r.evidence.trace && <button className="btn small" onClick={() => openPerformance(app, sid, dbid, "history")}>{t("Open the History tab")}</button>}</span>
          </div></div>
          <div className="mt-row ui-bar">
            <span>{t("{n} unused, {u} used, {c} enforcing a constraint, {i} inactive", { n: r.counts.unused, u: r.counts.used, c: r.counts.constraint, i: r.counts.inactive })}</span>
            <label className="check"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> {t("Show every index")}</label>
            {r.script && <>
              <button className="btn small" onClick={() => app.openQueryTool(sid, dbid, r.script)}>{t("Open the DROP statements in the Query Tool")}</button>
              <button className="btn small" onClick={() => openChanges(app, sid, dbid, undefined, r.script)}>{t("Propose as a change request…")}</button>
            </>}
          </div>
          {rows.length === 0 ? <div className="muted">{all ? t("No user indices.") : t("No unused index in this period.")}</div> : (
            <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Indices")}><table className="grid">
              <thead><tr><th>{t("Index")}</th><th>{t("Table")}</th><th>{t("Columns")}</th><th>{t("Status")}</th><th>{t("Last used")}</th>
                <th className="num">{t("Table writes")}</th><th className="num">{t("Entries")}</th></tr></thead>
              <tbody>{rows.map((x) => (
                <tr key={`${x.schema ?? ""}.${x.name}`}>
                  <td className="mono-cell">{x.schema ? `${x.schema}.` : ""}{x.name}{x.unique && <span className="muted"> · {t("unique")}</span>}</td>
                  <td>{x.table}</td>
                  <td className="mono-cell">{x.expression ? t("(expression)") : x.columns.join(", ")}</td>
                  <td><span className={"pill " + STATUS[x.status][1]}>{t(STATUS[x.status][0])}</span>{x.constraint && <span className="muted"> {x.constraint}</span>}</td>
                  <td>{x.last_used ? new Date(x.last_used * 1000).toLocaleString() : "–"}</td>
                  <td className="num">{fmt(x.table_writes)}</td><td className="num">{fmt(x.nodes)}</td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
          <p className="muted ui-note">{r.writes_known ? t("Table writes: inserts, updates and deletes since the database was opened.") : t("Table writes are not available on this server.")}{" "}
            {r.gstat_at ? t("Entries: from the gstat of the last check-up.") : t("Entries: run a check-up with gstat to see them.")}</p>
        </>}
      </Section>
    </div>
  );
}

export function openUnusedIndices(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "unused-indices", title: t("{db} · unused indices", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("unused-indices", {
  icon: <span aria-hidden="true" style={{ fontSize: 12 }}>⌧</span>,
  render: (tab, visible) => <UnusedTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Unused indices…"), onClick: () => openUnusedIndices(app, sid, dbid) }];
});

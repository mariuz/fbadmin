/** Milestone 0.7 - monitoring and performance: Performance tab, Profiler tab, dashboard health section. */
import { useEffect, useState } from "react";
import { registerDashboardSection, registerMenu, registerTabKind } from "../../registry";
import { useApp } from "../../context";
import type { AppApi } from "../../context";
import type { Selection } from "../../types";
import { IconActivity, IconPlan } from "../../components/Icons";
import { PerformanceTab } from "./Performance";
import { ProfilerTab } from "./Profiler";
import { fmtNum, monApi } from "./api";
import "./monitoring.css";
import { t } from "../../i18n";

const dbName = (app: AppApi, sid: string, dbid: string) =>
  app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? dbid;

export function openPerformance(app: AppApi, sid: string, dbid: string, sub?: string) {
  app.openTab({ kind: "performance", title: t("{db} · performance", { db: dbName(app, sid, dbid) }), serverId: sid, dbId: dbid, props: { sub } });
}
export function openProfiler(app: AppApi, sid: string, dbid: string, sql?: string) {
  app.openTab({ kind: "profiler", title: t("{db} · profiler", { db: dbName(app, sid, dbid) }), serverId: sid, dbId: dbid, props: { sql } });
}

registerTabKind("performance", {
  icon: <IconActivity size={13} />,
  render: (tab, visible) => <PerformanceTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} initial={tab.props?.sub} />,
});
registerTabKind("profiler", {
  icon: <IconPlan size={13} />,
  // a run started from the Query Tool gets its own tab; otherwise reuse the database's profiler tab
  same: (a, b) => !b.props?.autorun && a.serverId === b.serverId && a.dbId === b.dbId && !a.props?.autorun,
  render: (tab, visible) => <ProfilerTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} initialSql={tab.props?.sql} autorun={tab.props?.autorun} />,
});

registerMenu((app, sel) => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  return [
    { header: t("Performance") },
    { label: t("Performance dashboard"), icon: <IconActivity />, onClick: () => openPerformance(app, sel.serverId, sel.dbId!) },
    { label: t("Transactions & OAT"), onClick: () => openPerformance(app, sel.serverId, sel.dbId!, "transactions") },
    { label: t("Index advisor"), onClick: () => openPerformance(app, sel.serverId, sel.dbId!, "advisor") },
    { label: t("Profiler"), icon: <IconPlan />, onClick: () => openProfiler(app, sel.serverId, sel.dbId!) },
  ];
});

registerMenu((app, sel) => {
  if (sel.kind !== "object" || !sel.dbId || !["procedures", "functions"].includes(sel.otype ?? "")) return [];
  const sql = sel.otype === "procedures" ? `SELECT * FROM ${sel.name}` : `SELECT ${sel.name}() FROM RDB$DATABASE`;
  return [{ label: t("Profile…"), icon: <IconPlan />, onClick: () => openProfiler(app, sel.serverId, sel.dbId!, sql) }];
});

function HealthSection(props: { sel: Selection }) {
  const app = useApp();
  const { serverId, dbId } = props.sel;
  const [data, setData] = useState<Record<string, any> | null>(null);
  useEffect(() => {
    setData(null);
    monApi.transactions(serverId, dbId!).then(setData).catch(() => {});
  }, [serverId, dbId]);
  return (
    <>
      <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>{t("Health")}
        <span style={{ flex: 1 }} />
        <button className="btn small" onClick={() => openPerformance(app, serverId, dbId!)}><IconActivity size={12} /> {t("Performance")}</button>
        <button className="btn small" onClick={() => openProfiler(app, serverId, dbId!)}><IconPlan size={12} /> {t("Profiler")}</button>
      </h3>
      {!data ? <div className="muted">…</div> : (
        <div className="alerts">
          {data.alerts.length === 0 && <div className="alert good"><span className="lvl">{t("OK")}</span><span>{t("No long-running transactions; next − OAT gap {gap}.", { gap: fmtNum(data.gaps.next_oat) })}</span></div>}
          {data.alerts.map((a: any, i: number) => (
            <div key={i} className={"alert " + a.level} style={{ cursor: "pointer" }} onClick={() => openPerformance(app, serverId, dbId!, "transactions")}>
              <span className="lvl">{a.level}</span><span>{a.message}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

registerDashboardSection({
  id: "monitoring-health",
  render: (sel) => (sel.kind === "database" && sel.dbId ? <HealthSection key={sel.serverId + sel.dbId} sel={sel} /> : null),
});

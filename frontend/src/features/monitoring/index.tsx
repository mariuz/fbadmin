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

const dbName = (app: AppApi, sid: string, dbid: string) =>
  app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? dbid;

export function openPerformance(app: AppApi, sid: string, dbid: string, sub?: string) {
  app.openTab({ kind: "performance", title: `${dbName(app, sid, dbid)} · performance`, serverId: sid, dbId: dbid, props: { sub } });
}
export function openProfiler(app: AppApi, sid: string, dbid: string, sql?: string) {
  app.openTab({ kind: "profiler", title: `${dbName(app, sid, dbid)} · profiler`, serverId: sid, dbId: dbid, props: { sql } });
}

registerTabKind("performance", {
  icon: <IconActivity size={13} />,
  render: (t, visible) => <PerformanceTab serverId={t.serverId!} dbId={t.dbId!} visible={visible} initial={t.props?.sub} />,
});
registerTabKind("profiler", {
  icon: <IconPlan size={13} />,
  // a run started from the Query Tool gets its own tab; otherwise reuse the database's profiler tab
  same: (a, b) => !b.props?.autorun && a.serverId === b.serverId && a.dbId === b.dbId && !a.props?.autorun,
  render: (t, visible) => <ProfilerTab serverId={t.serverId!} dbId={t.dbId!} visible={visible} initialSql={t.props?.sql} autorun={t.props?.autorun} />,
});

registerMenu((app, sel) => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  return [
    { header: "Performance" },
    { label: "Performance dashboard", icon: <IconActivity />, onClick: () => openPerformance(app, sel.serverId, sel.dbId!) },
    { label: "Transactions & OAT", onClick: () => openPerformance(app, sel.serverId, sel.dbId!, "transactions") },
    { label: "Index advisor", onClick: () => openPerformance(app, sel.serverId, sel.dbId!, "advisor") },
    { label: "Profiler", icon: <IconPlan />, onClick: () => openProfiler(app, sel.serverId, sel.dbId!) },
  ];
});

registerMenu((app, sel) => {
  if (sel.kind !== "object" || !sel.dbId || !["procedures", "functions"].includes(sel.otype ?? "")) return [];
  const sql = sel.otype === "procedures" ? `SELECT * FROM ${sel.name}` : `SELECT ${sel.name}() FROM RDB$DATABASE`;
  return [{ label: "Profile…", icon: <IconPlan />, onClick: () => openProfiler(app, sel.serverId, sel.dbId!, sql) }];
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
      <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>Health
        <span style={{ flex: 1 }} />
        <button className="btn small" onClick={() => openPerformance(app, serverId, dbId!)}><IconActivity size={12} /> Performance</button>
        <button className="btn small" onClick={() => openProfiler(app, serverId, dbId!)}><IconPlan size={12} /> Profiler</button>
      </h3>
      {!data ? <div className="muted">…</div> : (
        <div className="alerts">
          {data.alerts.length === 0 && <div className="alert good"><span className="lvl">OK</span><span>No long-running transactions; next − OAT gap {fmtNum(data.gaps.next_oat)}.</span></div>}
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

/** Roadmap 2.3: database timeline - schema changes, maintenance, backups, alerts, upgrades and audit entries
 *  of one database in one history, to answer "what changed before it broke?". */
import { useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { N_, t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconRefresh } from "../../components/Icons";
import { openSchemaHistory } from "../schema-history";
import { openMigrations } from "../migrations";
import "./timeline.css";
import { timelineApi, type Category, type Timeline, type TimelineEvent } from "./api";

const CATS: [Category, string][] = [
  ["schema", N_("Schema")], ["data", N_("Data")], ["backup", N_("Backups")], ["maintenance", N_("Maintenance")],
  ["upgrade", N_("Upgrades")], ["alert", N_("Alerts")], ["audit", N_("Audit log")],
];
const RANGES: [number, string][] = [
  [86400, N_("24 hours")], [7 * 86400, N_("7 days")], [30 * 86400, N_("30 days")], [90 * 86400, N_("90 days")],
  [365 * 86400, N_("1 year")], [0, N_("Everything")],
];
const STATUS: Record<string, [string, string]> = {
  failed: ["failed", N_("failed")], warning: ["running", N_("warning")], running: ["running", N_("running")],
  cancelled: ["cancelled", N_("cancelled")],
};

const IconTimeline = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
    <path d="M6 3v18M6 7h12M6 12h8M6 17h11" />
  </svg>
);

const day = (ts: number) => new Date(ts * 1000).toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const toLocalInput = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

function Event(props: { e: TimelineEvent; sid: string; dbid: string }) {
  const app = useApp();
  const { e } = props;
  const [open, setOpen] = useState(false);
  const st = STATUS[e.status];
  const more = !!(e.detail || e.error);
  const link = (() => {
    switch (e.ref.type) {
      case "schema_commit": return [t("Open schema history"), () => openSchemaHistory(app, props.sid, props.dbid)] as const;
      case "migrations": return [t("Open migrations"), () => openMigrations(app, props.sid, props.dbid)] as const;
      case "alert": return [t("Open alerts"), () => app.openTab({ kind: "alerts", title: t("Alerts") })] as const;
      case "schedule": return [t("Open schedules"), () => app.openTab({ kind: "schedules", title: t("Schedules") })] as const;
      case "audit": return [t("Open the audit log"), () => app.openTab({ kind: "security-audit", title: t("Audit log") })] as const;
      default: return null;
    }
  })();
  return (
    <li className={"tl-ev " + e.category + (e.status === "failed" ? " bad" : "")}>
      <span className="tl-time">{hm(e.ts)}</span>
      <span className="tl-dot" aria-hidden="true" />
      <div className="tl-body">
        <div className="tl-line">
          <span className="tl-cat">{t(CATS.find(([k]) => k === e.category)?.[1] ?? e.category)}</span>
          <span className="tl-title">{e.title}</span>
          {st && <span className={"pill " + st[0]}>{t(st[1])}</span>}
          {e.user && <span className="muted tl-user">{e.user}</span>}
        </div>
        {(more || link) && <div className="tl-actions">
          {more && <button className="btn small" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? t("Hide details") : t("Details")}</button>}
          {link && <button className="btn small" onClick={link[1]}>{link[0]}</button>}
        </div>}
        {open && <>
          {e.error && <div className="err">{e.error}</div>}
          {e.detail && <pre className="code-block tl-detail" tabIndex={0} aria-label={t("Details")}>{e.detail}</pre>}
        </>}
      </div>
    </li>
  );
}

function TimelineTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const { serverId: sid, dbId: dbid } = props;
  const [range, setRange] = useState(30 * 86400);
  const [before, setBefore] = useState("");
  const [q, setQ] = useState("");
  const [cats, setCats] = useState<Category[]>([]);
  const [queries, setQueries] = useState(false);
  const [data, setData] = useState<Timeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [limit, setLimit] = useState(300);

  const load = async () => {
    setLoading(true);
    const until = before ? new Date(before).getTime() / 1000 : undefined;
    const since = range ? (until ?? Date.now() / 1000) - range : undefined;
    try {
      setData(await timelineApi.get(sid, dbid, { since, until, q: q.trim() || undefined, category: cats.join(",") || undefined, limit, audit_queries: queries }));
      setError(null);
    } catch (e) { setError(errMsg(e)); }
    setLoading(false);
  };
  useEffect(() => { if (props.visible) load(); }, [props.visible, range, before, cats.join(","), queries, limit]);
  useEffect(() => { const h = window.setTimeout(() => props.visible && load(), 300); return () => window.clearTimeout(h); }, [q]);

  const groups = useMemo(() => {
    const out: [string, TimelineEvent[]][] = [];
    for (const e of data?.events ?? []) {
      const d = day(e.ts);
      if (!out.length || out[out.length - 1][0] !== d) out.push([d, []]);
      out[out.length - 1][1].push(e);
    }
    return out;
  }, [data]);
  const toggle = (c: Category) => setCats((cs) => (cs.includes(c) ? cs.filter((x) => x !== c) : [...cs, c]));

  return (
    <div className="tl">
      <div className="tl-head">
        <h3>{t("Timeline")}</h3>
        <label>{t("Period")}{" "}
          <select className="text" value={range} onChange={(e) => setRange(Number(e.target.value))}>
            {RANGES.map(([v, l]) => <option key={v} value={v}>{t(l)}</option>)}
          </select>
        </label>
        <label>{t("before")}{" "}
          <input className="text" type="datetime-local" value={before} max={toLocalInput(new Date())} onChange={(e) => setBefore(e.target.value)} />
        </label>
        {before && <button className="btn small" onClick={() => setBefore("")}>{t("Until now")}</button>}
        <input className="text tl-search" type="search" aria-label={t("Search the timeline")} placeholder={t("Search titles, SQL and users…")} value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn small" onClick={load} aria-label={t("Refresh")} disabled={loading}><IconRefresh size={12} /></button>
      </div>
      <p className="muted tl-hint">{t("Everything that happened to this database: schema changes and migrations, data changes made in fbadmin, backups and test restores, maintenance, engine upgrades, alerts and, for administrators, the audit log. Pick a time in \"before\" to see what changed before something broke.")}</p>
      <div className="tl-cats" role="group" aria-label={t("Show")}>
        {CATS.filter(([k]) => k !== "audit" || data?.audit).map(([k, l]) => (
          <button key={k} className={"tl-chip " + k + (cats.includes(k) ? " on" : "")} aria-pressed={cats.includes(k)} onClick={() => toggle(k)}>
            {t(l)} <span className="n">{data?.counts?.[k] ?? 0}</span>
          </button>
        ))}
        {cats.length > 0 && <button className="btn small" onClick={() => setCats([])}>{t("Show all")}</button>}
        {data?.audit && <label className="check"><input type="checkbox" checked={queries} onChange={(e) => setQueries(e.target.checked)} /> {t("Include queries from the audit log")}</label>}
      </div>
      {error && <div className="err">{error}</div>}
      {data?.problems.map((p) => <div key={p.source} className="tl-problem">{t("Could not read {source}: {error}", { source: p.source, error: p.error })}</div>)}
      {data && data.events.length === 0 && <div className="muted tl-empty">{t("Nothing happened in this period.")}</div>}
      {groups.map(([d, evs]) => (
        <section key={d} className="tl-day" aria-label={d}>
          <h4>{d}</h4>
          <ol className="tl-list">{evs.map((e) => <Event key={e.id} e={e} sid={sid} dbid={dbid} />)}</ol>
        </section>
      ))}
      {data?.has_more && <button className="btn small" onClick={() => setLimit(limit * 2)}>{t("Show more")}</button>}
      {data?.engine_version && <div className="muted tl-foot">{t("Firebird {version}, ODS {ods}", { version: data.engine_version, ods: data.ods ?? "?" })}</div>}
    </div>
  );
}

export function openTimeline(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "timeline", title: t("{db} · timeline", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("timeline", {
  icon: <IconTimeline size={13} />,
  render: (tab, visible) => <TimelineTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Timeline…"), onClick: () => openTimeline(app, sid, dbid) }];
});

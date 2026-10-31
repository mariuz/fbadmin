/** Roadmap 2.3: guided performance check-up - one ranked "why is it slow?" report with a fix action per finding. */
import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconActivity } from "../../components/Icons";
import { Check, Section } from "../maintenance/common";
import { fmtBytes, fmtTime, mapi, waitJob } from "../maintenance/api";
import { monApi } from "../monitoring/api";
import { openPerformance } from "../monitoring";
import { openRegressions } from "../regressions";
import "../maintenance/maintenance.css";
import "../monitoring/monitoring.css";
import "./checkup.css";
import "./messages";
import { checkupApi, type CheckupAction, type CheckupFinding, type CheckupReport, type CheckupState, type Severity } from "./api";

const SEV: Record<Severity, string> = { critical: "critical", warning: "warning", info: "info" };
const sevLabel = (s: Severity) => (s === "critical" ? t("critical") : s === "warning" ? t("warning") : t("info"));
const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString());
const vars = (p: Record<string, any>) => Object.fromEntries(Object.entries(p).filter(([, v]) => typeof v === "string" || typeof v === "number"));
const PERF_VIEWS = new Set(["transactions", "locks", "advisor", "history", "tables"]);
const MAINT_VIEWS: Record<string, string> = { gstat: "gstat", backup: "backup", nbackup: "nbackup", schedules: "schedules", properties: "properties" };

function Facts(props: { r: CheckupReport }) {
  const f = props.r.facts;
  const items: [string, string][] = [
    [t("Database size"), f.size_bytes != null ? fmtBytes(f.size_bytes) : "—"],
    [t("Page cache"), f.page_buffers != null ? `${num(f.page_buffers)} ${t("pages")} (${f.cache_bytes != null ? fmtBytes(f.cache_bytes) : "?"})` : "—"],
    [t("Cache misses"), f.cache_miss != null ? `${(f.cache_miss * 100).toFixed(1)}%` : "—"],
    [t("Sweep interval"), f.sweep_interval ? num(f.sweep_interval) : t("off")],
    [t("Next − OAT"), num(f.next_oat)],
    [t("OAT − OIT"), num(f.oat_oit)],
    [t("Indices"), num(f.indices)],
  ];
  if (f.server_mode) items.push([t("Server mode"), f.server_mode]);
  return (
    <div className="cu-facts">
      {items.map(([k, v]) => <div key={k}><span className="k">{k}</span><span className="v">{v}</span></div>)}
    </div>
  );
}

function Finding(props: { f: CheckupFinding; busy: boolean; onAction: (a: CheckupAction) => void }) {
  const { f } = props;
  return (
    <li className={"cu-item " + SEV[f.severity]}>
      <h5><span className="cu-rank">{f.rank}.</span><span className="cu-sev">{sevLabel(f.severity)}</span><span>{t(f.title, vars(f.params))}</span></h5>
      <p className="cu-advice">{t(f.advice, vars(f.params))}</p>
      {f.evidence.length > 0 && <ul className="cu-ev">{f.evidence.map((e, i) => (
        <li key={i}><span className="lbl">{t(e.label)}</span>{e.value && <span>{e.value}</span>}
          {e.sql && <pre className="code-block" tabIndex={0} aria-label={t("Statement")}>{e.sql}</pre>}</li>
      ))}</ul>}
      {f.actions.length > 0 && <div className="cu-actions">{f.actions.map((a, i) => (
        <button key={i} className={"btn small" + (i === 0 && a.kind !== "open" ? " primary" : "")} disabled={props.busy} onClick={() => props.onAction(a)}>
          {t(a.label, vars(a.params))}
        </button>
      ))}</div>}
    </li>
  );
}

function CheckupTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [state, setState] = useState<CheckupState | null>(null);
  const [gstat, setGstat] = useState(true);
  const [running, setRunning] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const load = async () => {
    try {
      const s = await checkupApi.last(sid, dbid);
      if (!alive.current) return s;
      setState(s); setError(null);
      if (s.job?.status === "running") setRunning(s.job.log[s.job.log.length - 1] ?? t("Running…"));
      return s;
    } catch (e) { setError(errMsg(e)); return null; }
  };

  const run = async (withGstat = gstat) => {
    setRunning(t("Starting…")); setError(null);
    try {
      const job = await checkupApi.run(sid, dbid, withGstat);
      const end = await waitJob(job.id, (j) => alive.current && setRunning(j.log?.[j.log.length - 1] ?? t("Running…")));
      if (end.status === "failed") setError(end.error ?? t("The check-up failed"));
    } catch (e) { setError(errMsg(e)); }
    if (!alive.current) return;
    setRunning(null);
    await load();
  };

  useEffect(() => {
    if (!props.visible || state) return;
    load().then((s) => {
      if (s?.job?.status === "running") waitJob(s.job.id).then(() => { if (alive.current) { setRunning(null); load(); } });
    });
  }, [props.visible]);

  const rerun = () => run(state?.report?.gstat ?? gstat);

  const doAction = async (a: CheckupAction) => {
    const p = a.params;
    if (a.confirm && !confirm(t(a.confirm, vars(p)))) return;
    const job = async (id: string, done: string) => {
      const end = await waitJob(id);
      if (end.status === "finished") app.toast(done, "ok");
      else app.toast(end.error ?? t("The job failed"), "error");
    };
    try {
      switch (a.kind) {
        case "open":
          if (PERF_VIEWS.has(p.target)) openPerformance(app, sid, dbid, p.target);
          else if (p.target === "regressions") openRegressions(app, sid, dbid);
          else if (MAINT_VIEWS[p.target]) {
            const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
            app.openTab({ kind: "maintenance", title: t("{name} · maintenance", { name: db?.name ?? "db" }), serverId: sid, dbId: dbid, props: { section: MAINT_VIEWS[p.target], nonce: Date.now() } });
          }
          return;
        case "query_tool":
          app.openQueryTool(sid, dbid, p.sql);
          return;
      }
      setBusy(true);
      switch (a.kind) {
        case "recompute_statistics":
          await job((await checkupApi.recompute(sid, dbid, p.indices)).id, t("Selectivity recomputed"));
          break;
        case "sweep":
          app.toast(t("Sweep started"), "ok");
          await job((await mapi.sweep(sid, dbid)).id, t("Sweep finished"));
          break;
        case "set_sweep_interval":
          await mapi.setProperties(sid, dbid, { sweep_interval: p.value });
          app.toast(t("Automatic sweep is on"), "ok");
          break;
        case "set_page_buffers":
          await mapi.setProperties(sid, dbid, { page_buffers: p.value });
          app.toast(t("Page buffers set; they take effect when the database is next opened"), "ok");
          break;
        case "track_history":
          await monApi.setTracking(sid, dbid, true);
          app.toast(t("History is recorded for this database"), "ok");
          break;
        case "disconnect":
          await api.killAttachment(sid, dbid, p.attachment_id);
          app.toast(t("Attachment {id} disconnected", { id: p.attachment_id }), "ok");
          break;
      }
    } catch (e) {
      app.toast(errMsg(e), "error");
      setBusy(false);
      return;
    }
    setBusy(false);
    if (alive.current) await rerun();
  };

  const r = state?.report;
  return (
    <div className="mt-body checkup">
      <Section wide title={t("Performance check-up")}
        hint={t("Why is it slow? One ranked report from what fbadmin collects: the oldest active transaction, sweep settings, the page cache, lock waits, index selectivity, record versions (gstat), query regressions and the index advisor. Each finding comes with a fix.")}
        right={<>
          <Check label={t("Include gstat (reads the whole database)")} checked={gstat} onChange={setGstat} disabled={!!running} />
          <button className="btn primary small" disabled={!!running || busy} onClick={() => run()}>{r ? t("Run again") : t("Run the check-up")}</button>
        </>}>
        {running && <div className="muted" role="status">{t("Checking…")} {running}</div>}
        {error && <div className="err">{error}</div>}
        {!r && !running && state && <div className="muted">{t("No check-up has run for this database yet.")}</div>}
        {r && <>
          <div className="mt-row cu-summary">
            {r.summary.critical > 0 && <span className="pill failed">{t("{n} critical", { n: r.summary.critical })}</span>}
            {r.summary.warning > 0 && <span className="pill running">{t("{n} warning(s)", { n: r.summary.warning })}</span>}
            {r.summary.info > 0 && <span className="pill cancelled">{t("{n} note(s)", { n: r.summary.info })}</span>}
            <span className="muted">{t("Checked {time} in {s} s", { time: fmtTime(r.generated_at), s: r.duration })}{r.gstat ? "" : ` · ${t("without gstat")}`}</span>
          </div>
          <Facts r={r} />
          {r.checks.filter((c) => c.status === "error").map((c) => (
            <div key={c.name} className="err">{t("Could not check {name}: {error}", { name: c.name, error: c.error ?? "" })}</div>
          ))}
        </>}
      </Section>
      {r && <Section wide title={t("Findings")}>
        {r.findings.length === 0
          ? <div className="alerts"><div className="alert good"><span className="lvl">{t("ok")}</span><span>{t("Nothing found that slows this database down.")}</span></div></div>
          : <ol className="cu-list" aria-label={t("Findings, most important first")}>
            {r.findings.map((f) => <Finding key={f.code} f={f} busy={busy || !!running} onAction={doAction} />)}
          </ol>}
      </Section>}
    </div>
  );
}

export function openCheckup(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "checkup", title: t("{db} · check-up", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("checkup", {
  icon: <IconActivity size={13} />,
  render: (tab, visible) => <CheckupTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Performance check-up…"), onClick: () => openCheckup(app, sid, dbid) }];
});

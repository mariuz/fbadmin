/** Roadmap 2.2: query regression tracking - statements that got slower or changed their plan for the worse. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconRefresh, IconTerminal } from "../../components/Icons";
import { PlanDiff } from "../../components/PlanGraph";
import { Section } from "../maintenance/common";
import { fmtTime } from "../maintenance/api";
import "../maintenance/maintenance.css";
import "../monitoring/monitoring.css";
import "./regressions.css";
import { regApi, type RegDetail, type RegSettings, type RegStatement } from "./api";

const sec = (v: number | null | undefined) => (v == null ? "—" : v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(2)} s`);

/** Runs over time, one colour per plan. */
function RunsChart(props: { d: RegDetail }) {
  const runs = props.d.runs;
  if (runs.length < 2) return null;
  const w = 640, h = 140, pad = 28;
  const t0 = runs[0].ts, t1 = runs[runs.length - 1].ts || t0 + 1;
  const max = Math.max(...runs.map((r) => r.seconds), 0.001);
  const plans = props.d.plans.map((p) => p.plan_hash);
  const x = (ts: number) => pad + ((ts - t0) / Math.max(1, t1 - t0)) * (w - 2 * pad);
  const y = (s: number) => h - pad + 4 - (s / max) * (h - pad - 10);
  return (
    <figure className="reg-chart">
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" role="img" aria-label={t("Duration of each run over time, by plan")}>
        <line x1={pad} y1={h - pad + 4} x2={w - pad} y2={h - pad + 4} className="reg-axis" />
        <text x={2} y={14} className="reg-label">{sec(max)}</text>
        <text x={pad} y={h - 4} className="reg-label">{new Date(t0 * 1000).toLocaleDateString()}</text>
        <text x={w - pad} y={h - 4} className="reg-label" textAnchor="end">{new Date(t1 * 1000).toLocaleDateString()}</text>
        {runs.map((r, i) => (
          <circle key={i} cx={x(r.ts)} cy={y(r.seconds)} r={3.5} className={"reg-dot p" + Math.max(0, plans.indexOf(r.plan_hash ?? "")) % 4 + (r.plan_hash ? "" : " noplan")}>
            <title>{`${new Date(r.ts * 1000).toLocaleString()} · ${sec(r.seconds)} · ${r.source}`}</title>
          </circle>
        ))}
      </svg>
      <figcaption className="muted">{plans.map((p, i) => <span key={p} className="reg-legend"><span className={"reg-sw p" + (i % 4)} aria-hidden="true" /> {t("plan {n}", { n: i + 1 })}</span>)}</figcaption>
    </figure>
  );
}

function Detail(props: { sid: string; dbid: string; h: string; onAccepted: () => void }) {
  const app = useApp();
  const [d, setD] = useState<RegDetail | null>(null);
  useEffect(() => { regApi.detail(props.sid, props.dbid, props.h).then(setD).catch((e) => app.toast(errMsg(e), "error")); }, [props.h]);
  if (!d) return null;
  const a = d.analysis;
  const planText = (hash: string | null | undefined) => d.plans.find((p) => p.plan_hash === hash)?.plan ?? "";
  return (
    <Section wide title={t("Statement")}>
      <pre className="code-block" tabIndex={0} aria-label={t("Statement")}>{d.example}</pre>
      <div className="mt-row">
        <button className="btn small" onClick={() => app.openQueryTool(props.sid, props.dbid, d.example)}><IconTerminal size={12} /> {t("Open in Query Tool")}</button>
        {a?.regression && <button className="btn small" onClick={async () => {
          if (!confirm(t("Accept its current plan and duration as normal? The comparison starts again from now."))) return;
          try { await regApi.accept(props.sid, props.dbid, props.h); app.toast(t("Accepted as the new baseline"), "ok"); props.onAccepted(); } catch (e) { app.toast(errMsg(e), "error"); }
        }}>{t("Accept as the new baseline…")}</button>}
        {d.accepted_at && <span className="muted">{t("Baseline restarted {time}", { time: fmtTime(d.accepted_at) })}</span>}
      </div>
      <RunsChart d={d} />
      {d.plans.length > 0 && <div className="grid-wrap"><table className="grid">
        <thead><tr><th>{t("Plan")}</th><th>{t("Runs")}</th><th>{t("Median")}</th><th>{t("First seen")}</th><th>{t("Last seen")}</th></tr></thead>
        <tbody>{d.plans.map((p, i) => (
          <tr key={p.plan_hash}><td>{t("plan {n}", { n: i + 1 })}{p.plan_hash === a?.baseline_plan ? ` (${t("baseline")})` : ""}{p.plan_hash === a?.recent_plan ? ` (${t("recent")})` : ""}</td>
            <td className="num">{p.runs}</td><td className="num">{sec(p.median)}</td><td>{fmtTime(p.first_seen)}</td><td>{fmtTime(p.last_seen)}</td></tr>
        ))}</tbody>
      </table></div>}
      {a && a.baseline_plan && a.recent_plan && a.baseline_plan !== a.recent_plan
        ? <PlanDiff left={planText(a.baseline_plan)} right={planText(a.recent_plan)} leftTitle={t("Baseline plan")} rightTitle={t("Recent plan")} />
        : d.plans.length === 1 && <pre className="code-block" tabIndex={0} aria-label={t("Plan")}>{d.plans[0].plan}</pre>}
    </Section>
  );
}

function SettingsForm(props: { value: RegSettings; onSaved: () => void }) {
  const app = useApp();
  const [s, setS] = useState(props.value);
  const num = (k: keyof RegSettings, label: string, step = "1") => (
    <><label className="l" htmlFor={"reg-" + k}>{label}</label>
      <input id={"reg-" + k} className="text" type="number" step={step} min={0} style={{ width: 110 }} value={s[k]} onChange={(e) => setS({ ...s, [k]: Number(e.target.value) })} /></>
  );
  return (
    <div className="form mform">
      {num("regression_factor", t("Slower when the median is at least (× the baseline)"), "0.1")}
      {num("regression_min_seconds", t("and at least (seconds more)"), "0.1")}
      {num("regression_recent_hours", t("Recent runs: the last (hours)"))}
      {num("regression_baseline_days", t("Baseline: the days before (days)"))}
      {num("regression_min_runs", t("Runs needed on each side"))}
      {num("regression_query_ms", t("Query Tool statements slower than (ms)"))}
      {num("slow_seconds", t("Sampler: statements running longer than (s)"))}
      {num("regression_keep_days", t("Keep runs for (days)"))}
      <span />
      <div className="mt-row"><button className="btn primary" onClick={async () => {
        try { await regApi.saveSettings(s); app.toast(t("Saved"), "ok"); props.onSaved(); } catch (e) { app.toast(errMsg(e), "error"); }
      }}>{t("Save")}</button></div>
    </div>
  );
}

function RegressionsTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [only, setOnly] = useState("flagged");
  const [data, setData] = useState<{ tracking: boolean; settings: RegSettings; regressions: number; statements: RegStatement[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [settings, setSettings] = useState(false);
  const [tick, setTick] = useState(0);
  const load = () => regApi.list(sid, dbid, only).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { if (props.visible) load(); }, [props.visible, only, tick]);
  return (
    <div className="mt-body reg">
      <Section wide title={t("Query regressions")}
        hint={t("Statements seen by the history sampler and slow Query Tool statements, with their plans over time. A statement regressed when its recent runs are much slower than its baseline, or its plan changed and it got slower.")}
        right={<><button className="btn small" aria-expanded={settings} onClick={() => setSettings(!settings)}>{t("Settings…")}</button>
          <button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button></>}>
        {data && !data.tracking && <div className="alerts"><div className="alert warning"><span className="lvl">{t("off")}</span><span>
          {t("History isn't recorded for this database, so no statements are tracked.")}{" "}
          <button className="btn small" onClick={async () => { try { await regApi.track(sid, dbid); load(); } catch (e) { app.toast(errMsg(e), "error"); } }}>{t("Record history for this database")}</button>
        </span></div></div>}
        {settings && data && <SettingsForm value={data.settings} onSaved={() => { setSettings(false); load(); }} />}
        {error && <div className="err">{error}</div>}
        <div className="mt-row">
          <div className="seg" role="group" aria-label={t("Show")}>
            {[["regressions", t("Regressions")], ["flagged", t("Regressions and changed plans")], ["all", t("All statements")]].map(([k, l]) => (
              <button key={k} className={only === k ? "on" : ""} aria-pressed={only === k} onClick={() => setOnly(k)}>{l}</button>
            ))}
          </div>
          {data && <span className="muted">{t("{n} regression(s)", { n: data.regressions })}</span>}
        </div>
        {data && data.statements.length === 0 && <div className="muted">{only === "all" ? t("No statements tracked yet.") : t("Nothing got worse.")}</div>}
        {data && data.statements.length > 0 && <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Tracked statements")} style={{ maxHeight: 360 }}><table className="grid">
          <thead><tr><th>{t("Statement")}</th><th>{t("Baseline")}</th><th>{t("Recent")}</th><th>{t("Change")}</th><th>{t("Runs")}</th><th>{t("Flags")}</th><th>{t("Last seen")}</th></tr></thead>
          <tbody>{data.statements.map((s) => (
            <tr key={s.sql_hash} className={sel === s.sql_hash ? "sel" : ""} tabIndex={0} style={{ cursor: "pointer" }} aria-selected={sel === s.sql_hash}
              onClick={() => setSel(s.sql_hash)} onKeyDown={(e) => e.key === "Enter" && setSel(s.sql_hash)}>
              <td className="mono-cell reg-sql" title={s.example}>{s.normalized}</td>
              <td className="num">{sec(s.baseline_median)}</td><td className="num">{sec(s.recent_median)}</td>
              <td className={"num" + (s.regression ? " err" : "")}>{s.ratio != null ? `${s.ratio}×` : "—"}</td>
              <td className="num">{s.baseline_runs} / {s.recent_runs}</td>
              <td>{s.flags.map((f) => <span key={f} className={"pill " + (s.regression ? "failed" : "cancelled")}>{f === "slower" ? t("slower") : t("plan changed")}</span>)}</td>
              <td>{fmtTime(s.last_seen)}</td>
            </tr>
          ))}</tbody>
        </table></div>}
      </Section>
      {sel && <Detail key={sel + tick} sid={sid} dbid={dbid} h={sel} onAccepted={() => setTick((x) => x + 1)} />}
    </div>
  );
}

export function openRegressions(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "query-regressions", title: t("{db} · query regressions", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("query-regressions", {
  icon: <span style={{ fontSize: 12 }} aria-hidden="true">↯</span>,
  render: (tab, visible) => <RegressionsTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Query regressions…"), onClick: () => openRegressions(app, sid, dbid) }];
});

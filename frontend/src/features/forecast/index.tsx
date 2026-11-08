/** Roadmap 2.4: capacity and transaction-ID forecasting - growth trends and the limits they run into. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconRefresh } from "../../components/Icons";
import { Section } from "../maintenance/common";
import { fmtBytes, fmtTime } from "../maintenance/api";
import { LineChart } from "../monitoring/LineChart";
import { monApi } from "../monitoring/api";
import "../maintenance/maintenance.css";
import "../monitoring/monitoring.css";
import "./forecast.css";
import { fcApi, type Forecast, type Sev } from "./api";

const num = (v: number | null | undefined, digits = 0) => (v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: digits }));
export function fmtDays(d: number | null | undefined): string {
  if (d == null) return t("never at the current rate");
  if (d < 1) return t("less than a day");
  if (d < 60) return t("{n} days", { n: Math.floor(d) });
  if (d < 730) return t("{n} months", { n: Math.floor(d / 30.4) });
  const y = d / 365.25;
  return y >= 1e6 ? t("more than a million years") : t("{n} years", { n: Math.floor(y).toLocaleString() });
}
const SEV_LABEL: Record<Sev, string> = { ok: "ok", warning: "warning", critical: "critical" };
const pill = (s: Sev) => (s === "ok" ? <span className="pill finished">{t("ok")}</span>
  : <span className={"pill " + (s === "critical" ? "failed" : "running")}>{t(SEV_LABEL[s])}</span>);

function Budget(props: { sid: string; dbid: string; f: Forecast; onSaved: () => void }) {
  const app = useApp();
  const [gb, setGb] = useState(props.f.size.budget ? String(+(props.f.size.budget / 1024 ** 3).toFixed(2)) : "");
  return (
    <div className="mt-row fc-budget">
      <label htmlFor="fc-budget">{t("Space budget (GB)")}</label>
      <input id="fc-budget" className="text" type="number" min={0} step="0.1" style={{ width: 110 }} value={gb}
        placeholder={t("none")} onChange={(e) => setGb(e.target.value)} />
      <button className="btn small" onClick={async () => {
        try {
          await fcApi.budget(props.sid, props.dbid, gb.trim() ? Math.round(Number(gb) * 1024 ** 3) : null);
          app.toast(t("Saved"), "ok"); props.onSaved();
        } catch (e) { app.toast(errMsg(e), "error"); }
      }}>{t("Save")}</button>
      <span className="muted">{t("The space set aside for this database on its disk (Firebird cannot tell how much is free).")}</span>
    </div>
  );
}

function ForecastTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [f, setF] = useState<Forecast | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = async (observe = false) => {
    setBusy(true);
    try { setF(await (observe ? fcApi.observe(sid, dbid) : fcApi.get(sid, dbid))); setError(null); } catch (e) { setError(errMsg(e)); }
    setBusy(false);
  };
  useEffect(() => { if (props.visible) load(); }, [props.visible]);
  const seqs = f?.sequences ?? [];
  const shownSeqs = seqs.filter((s) => s.table || s.severity !== "ok" || s.pct > 1).slice(0, 50);
  const formats = (f?.formats ?? []).slice(0, 10);
  return (
    <div className="mt-body fc">
      <Section wide title={t("Capacity forecast")}
        hint={t("Growth trends from observations recorded at most once an hour (by the history sampler and when this page is opened), projected to the limits they run into: the space budget, the transaction counter, the columns sequences feed and the 255 formats of a table.")}
        right={<>
          <button className="btn small" disabled={busy} onClick={() => load(true)}>{t("Record an observation now")}</button>
          <button className="btn small" disabled={busy} onClick={() => load()} aria-label={t("Refresh")}><IconRefresh size={12} /></button>
        </>}>
        {error && <div className="err">{error}</div>}
        {f && !f.enough && (
          <div className="alerts"><div className="alert warning"><span className="lvl">{t("collecting")}</span><span>
            {t("{n} observation(s) so far: trends need observations at least 6 hours apart within the last {days} days.", { n: f.observations, days: f.window_days })}{" "}
            {!f.tracked && <>{t("Record history for this database to have one every hour.")}{" "}
              <button className="btn small" onClick={async () => { try { await monApi.setTracking(sid, dbid, true); load(); } catch (e) { app.toast(errMsg(e), "error"); } }}>{t("Record history for this database")}</button></>}
          </span></div></div>
        )}
        {f && f.findings.length > 0 && <div className="alerts" role="list" aria-label={t("Findings")}>
          {f.findings.map((x) => <div key={x.key} role="listitem" className={"alert " + x.severity}><span className="lvl">{t(SEV_LABEL[x.severity])}</span><span>{x.title}</span></div>)}
        </div>}
        {f && f.enough && f.findings.length === 0 && <div className="alerts"><div className="alert good"><span className="lvl">{t("ok")}</span><span>{t("No limit is near at the current rates.")}</span></div></div>}
        {f && <div className="muted">{t("{n} observation(s) since {time}", { n: f.observations, time: fmtTime(f.first) })}</div>}
      </Section>

      {f && <div className="fc-cards">
        <Section title={t("Database size")}>
          <dl className="fc-kv">
            <dt>{t("Now")}</dt><dd>{fmtBytes(f.size.bytes)}</dd>
            <dt>{t("Growth")}</dt><dd>{f.size.per_day == null ? "—" : t("{size} per day", { size: fmtBytes(f.size.per_day) })}</dd>
            <dt>{t("In 30 days")}</dt><dd>{f.size.in_30 == null ? "—" : fmtBytes(f.size.in_30)}</dd>
            <dt>{t("In 90 days")}</dt><dd>{f.size.in_90 == null ? "—" : fmtBytes(f.size.in_90)}</dd>
            <dt>{t("In a year")}</dt><dd>{f.size.in_365 == null ? "—" : fmtBytes(f.size.in_365)}</dd>
            {f.size.budget != null && <><dt>{t("Budget")}</dt><dd>{fmtBytes(f.size.budget)} · {num(f.size.budget_pct, 1)} % · {pill(f.size.severity)}</dd>
              <dt>{t("Budget reached in")}</dt><dd>{fmtDays(f.size.days_left)}</dd></>}
          </dl>
          <Budget key={String(f.size.budget)} sid={sid} dbid={dbid} f={f} onSaved={() => load()} />
        </Section>
        <Section title={t("Transactions")}>
          <dl className="fc-kv">
            <dt>{t("Next transaction")}</dt><dd>{num(f.transactions.next)}</dd>
            <dt>{t("Per day")}</dt><dd>{num(f.transactions.per_day)}</dd>
            <dt>{t("Limit")}</dt><dd>{num(f.transactions.limit)} {f.transactions.limit > 2 ** 32 ? t("(48-bit, Firebird 3 and later)") : t("(31-bit)")}</dd>
            <dt>{t("Limit reached in")}</dt><dd>{fmtDays(f.transactions.days_left)} {pill(f.transactions.severity)}</dd>
          </dl>
        </Section>
      </div>}

      {f && f.size.series.length > 1 && <Section wide title={t("Size over time")}>
        <LineChart title={t("Database size")} times={f.size.series.map((p) => p[0])} longRange binary
          series={[{ key: "size", label: t("Size"), slot: 1, values: f.size.series.map((p) => p[1]) }]} format={(v) => (v == null ? "–" : fmtBytes(v))} />
      </Section>}

      {f && <Section wide title={t("Sequences")} hint={t("A sequence can count to 2^63, but the column it fills may stop sooner: an INTEGER column at 2,147,483,647. Identity columns are matched exactly; for other sequences, the single-column primary key of a table whose triggers use the sequence.")}>
        {shownSeqs.length === 0 ? <div className="muted">{t("No sequences.")}</div> : (
          <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Sequences")}><table className="grid">
            <thead><tr><th>{t("Sequence")}</th><th>{t("Feeds")}</th><th>{t("Value")}</th><th>{t("Per day")}</th><th>{t("Used")}</th><th>{t("Limit reached in")}</th><th>{t("Status")}</th></tr></thead>
            <tbody>{shownSeqs.map((s) => (
              <tr key={s.name}>
                <td className="mono-cell">{s.name}</td>
                <td>{s.table ? <>{s.table}.{s.column} <span className="muted">{s.type}{s.how === "trigger" ? ` · ${t("probably (trigger)")}` : ` · ${t("identity")}`}</span></> : <span className="muted">{s.type}</span>}</td>
                <td className="num">{num(s.value)}</td><td className="num">{num(s.per_day)}</td>
                <td className="num">{s.pct < 0.01 ? "< 0.01 %" : `${num(s.pct, 2)} %`}</td>
                <td>{fmtDays(s.days_left)}</td><td>{pill(s.severity)}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        {seqs.length > shownSeqs.length && <div className="muted">{t("{n} more sequence(s) feed no known column and are far from their limit.", { n: seqs.length - shownSeqs.length })}</div>}
      </Section>}

      {f && formats.length > 0 && <Section wide title={t("Table formats")} hint={t("Each ALTER TABLE that changes a table's columns adds a format; at 255 the table cannot be changed until a backup and restore renumbers them.")}>
        <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Table formats")}><table className="grid">
          <thead><tr><th>{t("Table")}</th><th>{t("Formats used")}</th><th>{t("Status")}</th></tr></thead>
          <tbody>{formats.map((x) => <tr key={x.table}><td>{x.table}</td><td className="num">{x.formats} / {x.limit}</td><td>{pill(x.severity)}</td></tr>)}</tbody>
        </table></div>
      </Section>}
    </div>
  );
}

export function openForecast(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "forecast", title: t("{db} · capacity forecast", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("forecast", {
  icon: <span aria-hidden="true" style={{ fontSize: 12 }}>📈</span>,
  render: (tab, visible) => <ForecastTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Capacity forecast…"), onClick: () => openForecast(app, sid, dbid) }];
});

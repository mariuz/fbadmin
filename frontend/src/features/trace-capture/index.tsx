/** Roadmap 2.4: continuous trace capture - an always-on trace session per database feeding the slow statement
 * log and query regressions. Shown in the History view of a database (monitoring/Performance.tsx). */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import "./trace-capture.css";

export interface CaptureState {
  enabled: boolean; threshold_ms: number; tracked: boolean; per_minute: number; psql?: boolean; routines?: number;
  state: "running" | "starting" | "waiting" | "stopped"; session_id: number | null; started_at: number | null;
  captured: number; slow: number; dropped: number; errors?: number; last_event_at: number | null; error: string | null; sessions: number;
  retry_at: number | null;
}
const url = (sid: string, dbid: string) => `/api/servers/${encodeURIComponent(sid)}/databases/${encodeURIComponent(dbid)}/monitoring/trace-capture`;
export interface ErrorGroup {
  code: number; message: string; category: "lock" | "constraint" | "error"; count: number; first: number; last: number;
  users: number; statements: number; example_sql: string | null; example_user: string | null; detail: string | null; new: boolean;
}
export interface ErrorLog { hours: number; total: number; lock: number; groups: ErrorGroup[]; hourly: { ts: number; count: number; lock: number }[] }
export const captureApi = {
  errors: (sid: string, dbid: string, hours: number) =>
    request<ErrorLog>("GET", `/api/servers/${encodeURIComponent(sid)}/databases/${encodeURIComponent(dbid)}/monitoring/errors?hours=${hours}`),
  get: (sid: string, dbid: string) => request<CaptureState>("GET", url(sid, dbid)),
  set: (sid: string, dbid: string, enabled: boolean, threshold_ms: number, psql = false) => request<CaptureState>("PUT", url(sid, dbid), { enabled, threshold_ms, psql }),
};

const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : "–");

export function TraceCapturePanel(props: { serverId: string; dbId: string; tracked: boolean }) {
  const app = useApp();
  const [st, setSt] = useState<CaptureState | null>(null);
  const [ms, setMs] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => captureApi.get(props.serverId, props.dbId).then((s) => { setSt(s); setMs((cur) => cur || String(s.threshold_ms)); }).catch(() => setSt(null));
  useEffect(() => { load(); const h = setInterval(load, 15000); return () => clearInterval(h); }, [props.serverId, props.dbId, props.tracked]);
  if (!st) return null;
  const save = async (enabled: boolean, psql = !!st.psql) => {
    setBusy(true);
    try {
      const s = await captureApi.set(props.serverId, props.dbId, enabled, Math.max(0, ms.trim() === "" || Number.isNaN(Number(ms)) ? st.threshold_ms : Number(ms)), psql);
      setSt(s);
      app.toast(enabled ? (s.state === "running" ? t("Continuous trace started") : t("Continuous trace turned on; the session is not running yet")) : t("Continuous trace turned off"), enabled && s.state !== "running" ? "error" : "ok");
    } catch (e) { app.toast(errMsg(e), "error"); }
    setBusy(false);
  };
  const pill = st.state === "running" ? "finished" : st.state === "waiting" ? "failed" : st.state === "starting" ? "running" : "cancelled";
  const label = { running: t("running"), starting: t("starting"), waiting: t("waiting to retry"), stopped: t("off") }[st.state];
  return (
    <div className="tcap" role="group" aria-label={t("Continuous trace")}>
      <div className="tcap-row">
        <label className="check">
          <input type="checkbox" checked={st.enabled} disabled={busy || (!props.tracked && !st.enabled)} onChange={(e) => save(e.target.checked)} />
          {t("Continuous trace")}
        </label>
        {st.enabled && <span className={"pill " + pill}>{label}</span>}
        <label htmlFor="tcap-ms">{t("Record statements slower than")}</label>
        <input id="tcap-ms" className="text" type="number" min={0} style={{ width: 90 }} value={ms} onChange={(e) => setMs(e.target.value)} />
        <span>{t("ms")}</span>
        {st.enabled && String(st.threshold_ms) !== ms && <button className="btn small" disabled={busy} onClick={() => save(true)}>{t("Apply")}</button>}
        <label className="check">
          <input type="checkbox" checked={!!st.psql} disabled={busy || (!props.tracked && !st.enabled)} onChange={(e) => save(st.enabled, e.target.checked)} />
          {t("Also procedures, functions and triggers")}
        </label>
      </div>
      <div className="muted">
        {!props.tracked && !st.enabled ? t("Record the history of this database to turn it on.")
          : t("A trace session reports every statement when it finishes, so none is missed between samples. It feeds the slow statements and query regressions; while it runs, the sampler and the Query Tool leave those to it.")}
      </div>
      {st.enabled && <div className="muted tcap-stats">
        {st.state === "running" && t("Session {id} since {time}: {n} statement(s) recorded, {slow} slow, last at {last}.", { id: st.session_id ?? "", time: when(st.started_at), n: st.captured, slow: st.slow, last: when(st.last_event_at) })}
        {st.state === "running" && st.psql && " " + t("{n} procedure, function and trigger run(s).", { n: st.routines ?? 0 })}
        {st.dropped > 0 && " " + t("{n} statement(s) dropped (more than {max} a minute).", { n: st.dropped, max: st.per_minute })}
        {st.error && <div className="err">{t("Last error: {error}", { error: st.error })}{st.retry_at ? " " + t("Next try at {time}.", { time: when(st.retry_at) }) : ""}</div>}
      </div>}
      {(st.enabled || !!st.errors) && <TraceErrors serverId={props.serverId} dbId={props.dbId} />}
    </div>
  );
}

const RANGES: [number, string][] = [[1, N_("1 h")], [24, N_("24 h")], [168, N_("7 d")], [720, N_("30 d")]];
const CAT: Record<ErrorGroup["category"], string> = { lock: N_("lock conflict"), constraint: N_("constraint"), error: N_("error") };

/** The errors the continuous trace saw, grouped by error code (monitoring/trace_errors.py). */
function TraceErrors(props: { serverId: string; dbId: string }) {
  const app = useApp();
  const [hours, setHours] = useState(24);
  const [log, setLog] = useState<ErrorLog | null>(null);
  useEffect(() => {
    const load = () => captureApi.errors(props.serverId, props.dbId, hours).then(setLog).catch(() => setLog(null));
    load();
    const h = setInterval(load, 30000);
    return () => clearInterval(h);
  }, [props.serverId, props.dbId, hours]);
  if (!log) return null;
  return (
    <div className="tcap-errors" role="region" aria-label={t("Errors seen by the trace")}>
      <div className="tcap-row">
        <strong>{t("Errors seen by the trace")}</strong>
        <span className="muted">{t("{n} error(s), {lock} lock conflict(s)", { n: log.total, lock: log.lock })}</span>
        <div className="seg">{RANGES.map(([h, l]) => <button key={h} className={hours === h ? "on" : ""} onClick={() => setHours(h)}>{t(l)}</button>)}</div>
      </div>
      {log.groups.length === 0 ? <div className="muted">{t("No errors in this period.")}</div> : (
        <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Errors by code")}><table className="grid">
          <thead><tr><th>{t("Error")}</th><th>{t("Kind")}</th><th className="num">{t("Count")}</th><th>{t("Last seen")}</th><th>{t("Users")}</th><th>{t("Statement")}</th></tr></thead>
          <tbody>{log.groups.map((g) => (
            <tr key={g.code}>
              <td title={g.detail ?? undefined}>{g.message} <span className="muted mono">{g.code}</span>{g.new && <> <span className="pill running">{t("new")}</span></>}</td>
              <td><span className={"pill " + (g.category === "lock" ? "failed" : g.category === "constraint" ? "running" : "cancelled")}>{t(CAT[g.category])}</span></td>
              <td className="num">{g.count}</td><td>{when(g.last)}</td>
              <td>{g.users}{g.example_user ? ` · ${g.example_user}` : ""}</td>
              <td>{g.example_sql ? <><span className="mono-cell tcap-sql" title={g.example_sql}>{g.example_sql}</span>{" "}
                <button className="btn small" onClick={() => app.openQueryTool(props.serverId, props.dbId, g.example_sql!)}>{t("Open")}</button></>
                : <span className="muted" title={t("Firebird reports the statement only when it ran longer than the trace's threshold")}>{t("not recorded")}</span>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </div>
  );
}

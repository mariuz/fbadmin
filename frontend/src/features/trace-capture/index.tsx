/** Roadmap 2.4: continuous trace capture - an always-on trace session per database feeding the slow statement
 * log and query regressions. Shown in the History view of a database (monitoring/Performance.tsx). */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import "./trace-capture.css";

export interface CaptureState {
  enabled: boolean; threshold_ms: number; tracked: boolean; per_minute: number;
  state: "running" | "starting" | "waiting" | "stopped"; session_id: number | null; started_at: number | null;
  captured: number; slow: number; dropped: number; last_event_at: number | null; error: string | null; sessions: number;
  retry_at: number | null;
}
const url = (sid: string, dbid: string) => `/api/servers/${encodeURIComponent(sid)}/databases/${encodeURIComponent(dbid)}/monitoring/trace-capture`;
export const captureApi = {
  get: (sid: string, dbid: string) => request<CaptureState>("GET", url(sid, dbid)),
  set: (sid: string, dbid: string, enabled: boolean, threshold_ms: number) => request<CaptureState>("PUT", url(sid, dbid), { enabled, threshold_ms }),
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
  const save = async (enabled: boolean) => {
    setBusy(true);
    try {
      const s = await captureApi.set(props.serverId, props.dbId, enabled, Math.max(1, Number(ms) || st.threshold_ms));
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
        <input id="tcap-ms" className="text" type="number" min={1} style={{ width: 90 }} value={ms} onChange={(e) => setMs(e.target.value)} />
        <span>{t("ms")}</span>
        {st.enabled && String(st.threshold_ms) !== ms && <button className="btn small" disabled={busy} onClick={() => save(true)}>{t("Apply")}</button>}
      </div>
      <div className="muted">
        {!props.tracked && !st.enabled ? t("Record the history of this database to turn it on.")
          : t("A trace session reports every statement when it finishes, so none is missed between samples. It feeds the slow statements and query regressions; while it runs, the sampler and the Query Tool leave those to it.")}
      </div>
      {st.enabled && <div className="muted tcap-stats">
        {st.state === "running" && t("Session {id} since {time}: {n} statement(s) recorded, {slow} slow, last at {last}.", { id: st.session_id ?? "", time: when(st.started_at), n: st.captured, slow: st.slow, last: when(st.last_event_at) })}
        {st.dropped > 0 && " " + t("{n} statement(s) dropped (more than {max} a minute).", { n: st.dropped, max: st.per_minute })}
        {st.error && <div className="err">{t("Last error: {error}", { error: st.error })}{st.retry_at ? " " + t("Next try at {time}.", { time: when(st.retry_at) }) : ""}</div>}
      </div>}
    </div>
  );
}

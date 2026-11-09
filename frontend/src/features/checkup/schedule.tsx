/** Scheduled performance check-up (roadmap 2.4): the options of a "checkup" schedule, the Schedule… dialog of the
 * check-up page and the one-line result of a run. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import { Modal } from "../../components/Modal";
import { Check } from "../maintenance/common";
import { fmtTime, mapi, waitJob, type CheckupScheduleOptions, type HistoryItem, type Schedule } from "../maintenance/api";

export const defaultCheckupOptions = (): CheckupScheduleOptions => ({ gstat: true, min_severity: "warning", fix_selectivity: false, ignore: [] });

/** Findings worth alerting on that someone may want to leave out (their codes, see monitoring/checkup.py). */
const CODES: [string, string][] = [
  ["oat_stuck", N_("Stuck oldest active transaction")], ["long_transactions", N_("Long transactions")],
  ["oit_stuck", N_("Oldest interesting transaction behind")], ["page_cache", N_("Page cache too small")],
  ["lock_waits_now", N_("Statements waiting for locks")], ["lock_contention", N_("Lock contention")],
  ["selectivity_never", N_("Indices without selectivity")], ["selectivity_outdated", N_("Outdated selectivity")],
  ["inactive_indices", N_("Inactive indices")], ["record_versions", N_("Many record versions")],
  ["regressions", N_("Query regressions")], ["missing_indices", N_("Missing indices (index advisor)")],
  ["nbackup_locked", N_("Locked by nbackup")],
];
const PRESETS: [string, string][] = [
  ["0 6 * * *", N_("every day at 6:00")], ["0 */4 * * *", N_("every 4 hours")], ["0 * * * *", N_("every hour")],
  ["0 6 * * 1", N_("Mondays at 6:00")],
];

export function CheckupOptionsForm(props: { value: CheckupScheduleOptions; onChange: (v: CheckupScheduleOptions) => void }) {
  const v = props.value;
  const set = (p: Partial<CheckupScheduleOptions>) => props.onChange({ ...v, ...p });
  return (
    <div className="form mform">
      <label className="l" htmlFor="cus-sev">{t("Alert on")}</label>
      <select id="cus-sev" className="text" value={v.min_severity} onChange={(e) => set({ min_severity: e.target.value as CheckupScheduleOptions["min_severity"] })}>
        <option value="warning">{t("new warnings and critical findings")}</option>
        <option value="critical">{t("new critical findings only")}</option>
      </select>
      <span className="l" />
      <div className="muted" style={{ fontSize: 12 }}>{t("A message is sent (to the rules on the event \"checkup.findings\") when a finding appears, gets more severe or names objects it did not name in the previous run, and once when they are all gone.")}</div>
      <span className="l" />
      <div>
        <Check label={t("Include gstat (reads the whole database)")} checked={v.gstat} onChange={(gstat) => set({ gstat })} />
        <Check label={t("Recompute missing or outdated selectivity at once")} checked={v.fix_selectivity} onChange={(fix_selectivity) => set({ fix_selectivity })} />
      </div>
      <span className="l">{t("Never alert on")}</span>
      <div className="cu-ignore">
        {CODES.map(([code, label]) => (
          <Check key={code} label={t(label)} checked={v.ignore.includes(code)}
            onChange={(on) => set({ ignore: on ? [...v.ignore, code] : v.ignore.filter((c) => c !== code) })} />
        ))}
      </div>
    </div>
  );
}

/** One-line result of a scheduled check-up in the history. */
export function checkupResult(h: HistoryItem): string {
  const parts: string[] = [];
  const s = h.summary as Record<string, number> | undefined;
  if (s) parts.push(t("{c} critical, {w} warning(s), {i} note(s)", { c: s.critical ?? 0, w: s.warning ?? 0, i: s.info ?? 0 }));
  if (h.new?.length) parts.push(t("{n} new: {codes}", { n: h.new.length, codes: h.new.map((c) => h.alerted?.[c]?.title ?? c).join("; ") }));
  if (h.fixed?.length) parts.push(t("selectivity recomputed for {n} index(es)", { n: h.fixed.length }));
  if (h.check_errors?.length) parts.push(t("could not check: {names}", { names: h.check_errors.join(", ") }));
  return parts.join(" · ") || t("ok");
}

export function ScheduleCheckupDialog(props: { serverId: string; dbId: string; dbName: string; onClose: () => void; onRan?: () => void }) {
  const app = useApp();
  const [mine, setMine] = useState<Schedule[] | null>(null);
  const [name, setName] = useState(t("Daily check-up {name}", { name: props.dbName }));
  const [cron, setCron] = useState("0 6 * * *");
  const [opts, setOpts] = useState(defaultCheckupOptions());
  const [preview, setPreview] = useState<string[] | string>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => mapi.schedules().then((all) => setMine(all.filter((s) => s.kind === "checkup" && s.server_id === props.serverId && s.database_id === props.dbId))).catch(() => setMine([]));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const h = setTimeout(() => mapi.cronPreview(cron).then((r) => setPreview(r.next)).catch((e) => setPreview(errMsg(e))), 250);
    return () => clearTimeout(h);
  }, [cron]);
  const save = async () => {
    try {
      await mapi.createSchedule({ name: name.trim() || props.dbName, kind: "checkup", server_id: props.serverId, database_id: props.dbId, cron, enabled: true, checkup: opts } as never);
      app.toast(t("Scheduled"), "ok");
      load();
    } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const runNow = async (s: Schedule) => {
    try {
      setBusy(t("Running {name}…", { name: s.name }));
      const end = await waitJob((await mapi.runSchedule(s.id)).id);
      app.toast(end.status === "finished" ? t("Run finished") : end.error ?? t("The run failed"), end.status === "finished" ? "ok" : "error");
      load();
      props.onRan?.();
    } catch (e) { app.toast(errMsg(e), "error"); }
    setBusy(null);
  };
  return (
    <Modal title={t("Schedule the check-up of {name}", { name: props.dbName })} onClose={props.onClose} wide>
      <p className="muted">{t("fbadmin runs the check-up on the schedule and alerts only on what is new since the previous run. The last run's report is shown on the check-up page.")}</p>
      {mine && mine.length > 0 && <>
        <h4>{t("Scheduled check-ups of this database")}</h4>
        <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Scheduled check-ups of this database")}><table className="grid">
          <thead><tr><th>{t("Name")}</th><th>{t("Schedule")}</th><th>{t("Next run")}</th><th>{t("Last run")}</th><th /></tr></thead>
          <tbody>{mine.map((s) => (
            <tr key={s.id}><td>{s.enabled ? "" : "⏸ "}{s.name}</td><td className="mono">{s.cron}</td><td>{s.enabled ? fmtTime(s.next_run) : t("disabled")}</td>
              <td>{s.last_run ? <><span className={"pill " + (s.last_status ?? "")}>{t(s.last_status ?? "")}</span> {fmtTime(s.last_run)}</> : "—"}</td>
              <td><button className="btn small" disabled={!!busy} onClick={() => runNow(s)}>{t("Run now")}</button></td></tr>
          ))}</tbody>
        </table></div>
        <div className="mt-row"><button className="btn small" onClick={() => { props.onClose(); app.openTab({ kind: "schedules", title: t("Schedules") }); }}>{t("Open the schedules")}</button></div>
      </>}
      {busy && <div className="muted" role="status">{busy}</div>}
      <h4>{t("New schedule")}</h4>
      <div className="form mform">
        <label className="l" htmlFor="cus-name">{t("Name")}</label>
        <input id="cus-name" className="text" value={name} onChange={(e) => setName(e.target.value)} />
        <label className="l" htmlFor="cus-cron">{t("When (cron)")}</label>
        <div>
          <div className="mt-row">
            <input id="cus-cron" className="text mono" style={{ width: 160 }} value={cron} onChange={(e) => setCron(e.target.value)} />
            <select className="text" aria-label={t("presets…")} value="" onChange={(e) => e.target.value && setCron(e.target.value)}>
              <option value="">{t("presets…")}</option>
              {PRESETS.map(([c, l]) => <option key={c} value={c}>{t(l)}</option>)}
            </select>
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            {typeof preview === "string" ? <span className="err">{preview}</span> : t("Next: {times}", { times: preview.map((p) => p.replace("T", " ")).join(" · ") })}
          </div>
        </div>
      </div>
      <CheckupOptionsForm value={opts} onChange={setOpts} />
      <div className="mt-row" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={typeof preview === "string"} onClick={save}>{t("Schedule it")}</button>
      </div>
    </Modal>
  );
}

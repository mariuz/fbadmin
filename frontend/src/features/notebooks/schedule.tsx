/** Scheduled notebooks (roadmap 2.4): the options of a "notebook" schedule, and the Schedule… dialog of a notebook. */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import { Modal } from "../../components/Modal";
import { fmtTime, mapi, waitJob, type NotebookScheduleOptions, type Schedule } from "../maintenance/api";
import { nbApi, type NotebookMeta } from "./api";

export interface DeliveryChannel { id: string; name: string; kind: string; enabled: boolean }
export const defaultNotebookOptions = (notebook_id = ""): NotebookScheduleOptions =>
  ({ notebook_id, channels: [], send: "always", read_only: true, save_results: true, attach_csv: true });

const SEND: [NotebookScheduleOptions["send"], string][] = [
  ["always", N_("after every run")], ["rows", N_("only when a result has rows")], ["error", N_("only when a cell fails")],
];
export const NB_PRESETS: [string, string][] = [
  ["0 7 * * 1-5", N_("weekdays at 7:00")], ["0 7 * * *", N_("every day at 7:00")], ["0 7 * * 1", N_("Mondays at 7:00")],
  ["0 7 1 * *", N_("the 1st of the month at 7:00")], ["0 * * * *", N_("every hour")],
];

/** The options of a notebook schedule: which notebook, where the report goes, when, and how it runs. */
export function NotebookOptionsForm(props: { value: NotebookScheduleOptions; onChange: (v: NotebookScheduleOptions) => void;
  serverId: string; databaseId: string; fixedNotebook?: boolean }) {
  const { value: v, onChange } = props;
  const [notebooks, setNotebooks] = useState<NotebookMeta[] | null>(null);
  const [channels, setChannels] = useState<DeliveryChannel[] | null>(null);
  useEffect(() => {
    if (!props.fixedNotebook) nbApi.list({ server_id: props.serverId, database_id: props.databaseId }).then((r) => setNotebooks(r.notebooks)).catch(() => setNotebooks([]));
  }, [props.serverId, props.databaseId]);
  useEffect(() => { request<{ channels: DeliveryChannel[] }>("GET", "/api/notebooks/channels").then((r) => setChannels(r.channels)).catch(() => setChannels([])); }, []);
  const set = (p: Partial<NotebookScheduleOptions>) => onChange({ ...v, ...p });
  return (
    <div className="form mform">
      {!props.fixedNotebook && <>
        <label className="l" htmlFor="nbs-nb">{t("Notebook")}</label>
        <select id="nbs-nb" className="text" value={v.notebook_id} onChange={(e) => set({ notebook_id: e.target.value })}>
          <option value="">{notebooks?.length === 0 ? t("no notebook on this database") : t("choose…")}</option>
          {notebooks?.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
        </select>
      </>}
      <span className="l">{t("Send the report to")}</span>
      <div className="nbs-channels" role="group" aria-label={t("Send the report to")}>
        {channels?.length === 0 && <span className="muted">{t("No alert channels yet: an fbadmin administrator adds e-mail, Slack or webhook channels under Alerts. Without one the run only refreshes the notebook's results.")}</span>}
        {channels?.map((c) => (
          <label key={c.id} className="check">
            <input type="checkbox" checked={v.channels.includes(c.id)} disabled={!c.enabled}
              onChange={(e) => set({ channels: e.target.checked ? [...v.channels, c.id] : v.channels.filter((x) => x !== c.id) })} />
            {" "}{c.name} <span className="muted">({c.kind}{c.enabled ? "" : ", " + t("disabled")})</span>
          </label>
        ))}
      </div>
      <label className="l" htmlFor="nbs-send">{t("Send it")}</label>
      <select id="nbs-send" className="text" value={v.send} onChange={(e) => set({ send: e.target.value as NotebookScheduleOptions["send"] })}>
        {SEND.map(([k, l]) => <option key={k} value={k}>{t(l)}</option>)}
      </select>
      <span />
      <label className="check" title={t("Cells that change data or metadata are refused. Turn this off for a runbook that must write (a month-end close).")}>
        <input type="checkbox" checked={v.read_only} onChange={(e) => set({ read_only: e.target.checked })} /> {t("Read only (refuse cells that change data)")}
      </label>
      <span />
      <label className="check"><input type="checkbox" checked={v.save_results} onChange={(e) => set({ save_results: e.target.checked })} /> {t("Keep the results in the notebook")}</label>
      <span />
      <label className="check"><input type="checkbox" checked={v.attach_csv} onChange={(e) => set({ attach_csv: e.target.checked })} /> {t("E-mail: attach each result as CSV")}</label>
    </div>
  );
}

/** A notebook's own schedules and a form for a new one. */
export function ScheduleDialog(props: { nb: { id: string; name: string; server_id: string | null; database_id: string | null }; onClose: () => void }) {
  const app = useApp();
  const { nb } = props;
  const [mine, setMine] = useState<Schedule[] | null>(null);
  const [name, setName] = useState(t("{name} report", { name: nb.name }));
  const [cron, setCron] = useState("0 7 * * 1-5");
  const [opts, setOpts] = useState(defaultNotebookOptions(nb.id));
  const [preview, setPreview] = useState<string[] | string>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => mapi.schedules().then((all) => setMine(all.filter((s) => s.kind === "notebook" && s.notebook?.notebook_id === nb.id))).catch(() => setMine([]));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const h = setTimeout(() => mapi.cronPreview(cron).then((r) => setPreview(r.next)).catch((e) => setPreview(errMsg(e))), 250);
    return () => clearTimeout(h);
  }, [cron]);
  if (!nb.server_id || !nb.database_id) {
    return <Modal title={t("Schedule {name}", { name: nb.name })} onClose={props.onClose}><p>{t("Pick the database this notebook runs on first.")}</p></Modal>;
  }
  const save = async () => {
    try {
      await mapi.createSchedule({ name: name.trim() || nb.name, kind: "notebook", server_id: nb.server_id!, database_id: nb.database_id!, cron, enabled: true, notebook: opts } as never);
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
    } catch (e) { app.toast(errMsg(e), "error"); }
    setBusy(null);
  };
  return (
    <Modal title={t("Schedule {name}", { name: nb.name })} onClose={props.onClose} wide>
      <p className="muted">{t("fbadmin runs the notebook's SQL cells on the schedule with your fbadmin permissions, keeps the results in the notebook and sends a report (the notebook as Markdown, the first rows, and on e-mail each result as CSV).")}</p>
      {mine && mine.length > 0 && <>
        <h4>{t("Schedules of this notebook")}</h4>
        <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Schedules of this notebook")}><table className="grid">
          <thead><tr><th>{t("Name")}</th><th>{t("Schedule")}</th><th>{t("Next run")}</th><th>{t("Last run")}</th><th /></tr></thead>
          <tbody>{mine.map((s) => (
            <tr key={s.id}><td>{s.enabled ? "" : "⏸ "}{s.name}</td><td className="mono">{s.cron}</td><td>{s.enabled ? fmtTime(s.next_run) : t("disabled")}</td>
              <td>{s.last_run ? <><span className={"pill " + (s.last_status ?? "")}>{s.last_status}</span> {fmtTime(s.last_run)}</> : "—"}</td>
              <td><button className="btn small" disabled={!!busy} onClick={() => runNow(s)}>{t("Run now")}</button></td></tr>
          ))}</tbody>
        </table></div>
        <div className="mt-row"><button className="btn small" onClick={() => { props.onClose(); app.openTab({ kind: "schedules", title: t("Schedules") }); }}>{t("Open the schedules")}</button></div>
      </>}
      {busy && <div className="muted" role="status">{busy}</div>}
      <h4>{t("New schedule")}</h4>
      <div className="form mform">
        <label className="l" htmlFor="nbs-name">{t("Name")}</label>
        <input id="nbs-name" className="text" value={name} onChange={(e) => setName(e.target.value)} />
        <label className="l" htmlFor="nbs-cron">{t("When (cron)")}</label>
        <div>
          <div className="mt-row">
            <input id="nbs-cron" className="text mono" style={{ width: 160 }} value={cron} onChange={(e) => setCron(e.target.value)} />
            <select className="text" aria-label={t("presets…")} value="" onChange={(e) => e.target.value && setCron(e.target.value)}>
              <option value="">{t("presets…")}</option>
              {NB_PRESETS.map(([c, l]) => <option key={c} value={c}>{t(l)}</option>)}
            </select>
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            {typeof preview === "string" ? <span className="err">{preview}</span> : t("Next: {times}", { times: preview.map((p) => p.replace("T", " ")).join(" · ") })}
          </div>
        </div>
      </div>
      <NotebookOptionsForm value={opts} onChange={setOpts} serverId={nb.server_id} databaseId={nb.database_id} fixedNotebook />
      <div className="mt-row" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={typeof preview === "string"} onClick={save}>{t("Schedule it")}</button>
      </div>
    </Modal>
  );
}

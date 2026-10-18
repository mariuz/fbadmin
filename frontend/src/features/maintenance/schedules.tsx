import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import type { Job } from "../../types";
import { IconDownload, IconPlay, IconPlus, IconRefresh, IconTrash } from "../../components/Icons";
import { browserDownload, fmtBytes, fmtTime, mapi, type ArchiveItem, type HistoryItem, type Schedule } from "./api";
import { BackupOptionsForm, JobLog, Section, backupPayload, defaultBackupOpts, useBusy, useCaps, type BackupOpts } from "./common";

const PRESETS: Array<[string, string]> = [
  ["0 2 * * *", N_("daily at 02:00")], ["30 1 * * 1-5", N_("weekdays at 01:30")], ["0 3 * * 0", N_("Sundays at 03:00")],
  ["0 */6 * * *", N_("every 6 hours")], ["@hourly", N_("hourly")], ["0 4 1 * *", N_("monthly, 1st at 04:00")],
];

interface Form { id?: string; name: string; kind: Schedule["kind"]; server_id: string; database_id: string; cron: string; enabled: boolean; keep: string; options: BackupOpts }

function toForm(s: Schedule): Form {
  const o = { ...defaultBackupOpts() } as any;
  for (const [k, v] of Object.entries(s.options ?? {})) o[k] = v === null ? (typeof o[k] === "boolean" ? false : "") : typeof o[k] === "string" ? String(v) : v;
  return { id: s.id, name: s.name, kind: s.kind, server_id: s.server_id, database_id: s.database_id, cron: s.cron, enabled: s.enabled, keep: String(s.keep), options: o };
}

export function SchedulesPanel(props: { visible: boolean; serverId?: string; dbId?: string }) {
  const app = useApp();
  const [items, setItems] = useState<Schedule[]>([]);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [form, setForm] = useState<Form | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [backups, setBackups] = useState<ArchiveItem[]>([]);
  const [preview, setPreview] = useState<string[] | string>([]);
  const [job, setJob] = useState<Job | null>(null);
  const caps = useCaps(form?.server_id || props.serverId || app.servers[0]?.id || "");
  const { busy, error, run } = useBusy();

  const load = async () => {
    try {
      setItems(await mapi.schedules());
      setHistory(await mapi.scheduleHistory(selected ?? undefined));
      if (selected) setBackups(await mapi.scheduleBackups(selected));
    } catch (e) { app.toast(errMsg(e), "error"); }
  };
  useEffect(() => {
    if (!props.visible) return;
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [props.visible, selected]);
  useEffect(() => {
    if (!form) return;
    const timer = setTimeout(() => mapi.cronPreview(form.cron).then((r) => setPreview(r.next)).catch((e) => setPreview(errMsg(e))), 250);
    return () => clearTimeout(timer);
  }, [form?.cron]);

  const newForm = () => {
    const sid = props.serverId ?? app.servers[0]?.id ?? "";
    const srv = app.servers.find((s) => s.id === sid);
    const dbid = props.dbId ?? srv?.databases[0]?.id ?? "";
    const dbName = srv?.databases.find((d) => d.id === dbid)?.name ?? t("database");
    setForm({ name: t("Nightly backup {name}", { name: dbName }), kind: "backup", server_id: sid, database_id: dbid, cron: "0 2 * * *", enabled: true, keep: "7", options: defaultBackupOpts() });
  };
  useEffect(() => { if (props.dbId && props.visible && !form) newForm(); }, []);
  const save = () => run(async () => {
    if (!form) return;
    const body = { name: form.name, kind: form.kind, server_id: form.server_id, database_id: form.database_id, cron: form.cron, enabled: form.enabled, keep: Number(form.keep) || 0, options: backupPayload(form.options) };
    const s = form.id ? await mapi.updateSchedule(form.id, body) : await mapi.createSchedule(body);
    setForm(null);
    setSelected(s.id);
    await load();
  }, t("Schedule saved"));
  const srvOf = (id: string) => app.servers.find((s) => s.id === id);
  const dbName = (s: Schedule) => srvOf(s.server_id)?.databases.find((d) => d.id === s.database_id)?.name ?? t("(unregistered)");
  const fsrv = form ? srvOf(form.server_id) : undefined;
  return (
    <div className="mt-body">
      <Section wide title={t("Scheduled jobs")} hint={t("Backups (streamed into the fbadmin archive with retention), sweeps and online validations on a cron schedule, run by fbadmin itself. Times are in the fbadmin host's local time; runs missed while fbadmin was stopped are skipped. The server password must be saved.")}
        right={<><button className="btn small" onClick={load}><IconRefresh size={12} /></button><button className="btn small primary" onClick={newForm}><IconPlus size={12} /> {t("New schedule")}</button></>}>
        {items.length === 0 ? <div className="muted">{t("No schedules yet.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Kind")}</th><th>{t("Database")}</th><th>{t("Schedule")}</th><th>{t("Keep")}</th><th>{t("Next run")}</th><th>{t("Last run")}</th><th /></tr></thead>
            <tbody>{items.map((s) => (
              <tr key={s.id} className={selected === s.id ? "sel" : ""} onClick={() => setSelected(s.id)} style={{ cursor: "pointer" }}>
                <td>{s.enabled ? "" : "⏸ "}{s.name}</td><td>{s.kind}</td><td>{srvOf(s.server_id)?.name} / {dbName(s)}</td>
                <td className="mono">{s.cron}</td><td className="num">{s.kind === "backup" ? (s.keep || t("all")) : "—"}</td>
                <td>{s.enabled ? fmtTime(s.next_run) : t("disabled")}</td>
                <td>{s.last_run ? <><span className={"pill " + (s.last_status ?? "")}>{s.last_status}</span> {fmtTime(s.last_run)}</> : "—"}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  <button className="btn small" title={t("Run now")} disabled={busy} onClick={() => run(async () => { setJob(await mapi.runSchedule(s.id)); setSelected(s.id); })}><IconPlay size={12} /></button>{" "}
                  <button className="btn small" onClick={() => setForm(toForm(s))}>{t("Edit")}</button>{" "}
                  <button className="btn small" onClick={() => run(async () => { await mapi.updateSchedule(s.id, { enabled: !s.enabled }); await load(); })}>{s.enabled ? t("Disable") : t("Enable")}</button>{" "}
                  <button className="btn small danger" onClick={() => run(async () => {
                    if (!confirm(t("Delete schedule \"{name}\"?", { name: s.name }))) return;
                    const delFiles = s.kind === "backup" && confirm(t("Also delete its backup files from the archive?"));
                    await mapi.deleteSchedule(s.id, delFiles);
                    if (selected === s.id) setSelected(null);
                    await load();
                  })}><IconTrash size={12} /></button>
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        {error && <div className="err" style={{ marginTop: 8 }}>{error}</div>}
        <JobLog job={job} onDone={load} maxHeight={160} />
      </Section>
      {form && (
        <Section title={form.id ? t("Edit schedule") : t("New schedule")}>
          <div className="form mform">
            <label className="l">{t("Name")}</label>
            <input className="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <label className="l">{t("Job")}</label>
            <select className="text" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as Schedule["kind"] })}>
              <option value="backup">{t("Backup (gbak, into the fbadmin archive)")}</option><option value="sweep">{t("Sweep")}</option><option value="validate">{t("Online validation")}</option>
            </select>
            <label className="l">{t("Server / database")}</label>
            <div className="mt-row">
              <select className="text" value={form.server_id} onChange={(e) => setForm({ ...form, server_id: e.target.value, database_id: srvOf(e.target.value)?.databases[0]?.id ?? "" })}>
                {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <select className="text" style={{ flex: 1 }} value={form.database_id} onChange={(e) => setForm({ ...form, database_id: e.target.value })}>
                {fsrv?.databases.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            </div>
            <label className="l">{t("Cron (min hour dom mon dow)")}</label>
            <div>
              <div className="mt-row">
                <input className="text mono" style={{ width: 180 }} value={form.cron} onChange={(e) => setForm({ ...form, cron: e.target.value })} />
                <select className="text" value="" onChange={(e) => e.target.value && setForm({ ...form, cron: e.target.value })}>
                  <option value="">{t("presets…")}</option>
                  {PRESETS.map(([c, l]) => <option key={c} value={c}>{t(l)} ({c})</option>)}
                </select>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {typeof preview === "string" ? <span className="err">{preview}</span> : t("Next: {times}", { times: preview.map((p) => p.replace("T", " ")).join(" · ") })}
              </div>
            </div>
            {form.kind === "backup" && <>
              <label className="l">{t("Keep last N backups")}</label>
              <input className="text" type="number" min={0} value={form.keep} onChange={(e) => setForm({ ...form, keep: e.target.value })} title={t("0 keeps all")} />
            </>}
            <span />
            <label className="check"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> {t("Enabled")}</label>
          </div>
          {form.kind === "backup" && <><h3>{t("Backup options")}</h3><BackupOptionsForm value={form.options} onChange={(o) => setForm({ ...form, options: o })} caps={caps} /></>}
          <div className="mt-row" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy || !form.database_id || typeof preview === "string"} onClick={save}>{t("Save")}</button>
            <button className="btn" onClick={() => setForm(null)}>{t("Cancel")}</button>
          </div>
        </Section>
      )}
      {selected && items.find((s) => s.id === selected)?.kind === "backup" && (
        <Section title={t("Backups of this schedule")}>
          {backups.length === 0 ? <div className="muted">{t("No backups yet.")}</div> : (
            <div className="grid-wrap"><table className="grid">
              <thead><tr><th>{t("File")}</th><th>{t("Size")}</th><th>{t("Created")}</th><th /></tr></thead>
              <tbody>{backups.map((b) => {
                const sid = items.find((s) => s.id === selected)!.server_id;
                return (
                  <tr key={b.path}><td>{b.name}</td><td className="num">{fmtBytes(b.size)}</td><td>{fmtTime(b.modified)}</td>
                    <td><button className="btn small" onClick={() => browserDownload(mapi.archiveUrl(sid, b.path), b.name)}><IconDownload size={12} /> {t("Download")}</button></td></tr>
                );
              })}</tbody>
            </table></div>
          )}
        </Section>
      )}
      <Section title={selected ? t("History of this schedule") : t("History (all schedules)")} right={selected && <button className="btn small" onClick={() => setSelected(null)}>{t("Show all")}</button>}>
        {history.length === 0 ? <div className="muted">{t("No runs yet.")}</div> : (
          <div className="grid-wrap" style={{ maxHeight: 320 }}><table className="grid">
            <thead><tr><th>{t("Started")}</th><th>{t("Schedule")}</th><th>{t("Kind")}</th><th>{t("Status")}</th><th>{t("Duration")}</th><th>{t("Result")}</th></tr></thead>
            <tbody>{history.map((h, i) => (
              <tr key={i}>
                <td>{fmtTime(h.started_at)}</td><td>{h.schedule_name}</td><td>{h.kind}</td>
                <td><span className={"pill " + h.status}>{h.status}</span></td>
                <td className="num">{(h.finished_at - h.started_at).toFixed(1)} s</td>
                <td style={{ whiteSpace: "normal" }}>{h.error ? <span className="err">{h.error}</span> : h.file ? `${h.file} · ${fmtBytes(h.size)}${h.removed?.length ? " · " + t("{count} old removed", { count: h.removed.length }) : ""}` : t("ok")}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Section>
    </div>
  );
}

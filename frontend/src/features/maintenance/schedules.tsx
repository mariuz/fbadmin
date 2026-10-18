import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { Job } from "../../types";
import { IconDownload, IconPlay, IconPlus, IconRefresh, IconTrash } from "../../components/Icons";
import { browserDownload, fmtBytes, fmtTime, mapi, type ArchiveItem, type HistoryItem, type Schedule } from "./api";
import { BackupOptionsForm, JobLog, Section, backupPayload, defaultBackupOpts, useBusy, useCaps, type BackupOpts } from "./common";

const PRESETS: Array<[string, string]> = [
  ["0 2 * * *", "daily at 02:00"], ["30 1 * * 1-5", "weekdays at 01:30"], ["0 3 * * 0", "Sundays at 03:00"],
  ["0 */6 * * *", "every 6 hours"], ["@hourly", "hourly"], ["0 4 1 * *", "monthly, 1st at 04:00"],
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
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [props.visible, selected]);
  useEffect(() => {
    if (!form) return;
    const t = setTimeout(() => mapi.cronPreview(form.cron).then((r) => setPreview(r.next)).catch((e) => setPreview(errMsg(e))), 250);
    return () => clearTimeout(t);
  }, [form?.cron]);

  const newForm = () => {
    const sid = props.serverId ?? app.servers[0]?.id ?? "";
    const srv = app.servers.find((s) => s.id === sid);
    const dbid = props.dbId ?? srv?.databases[0]?.id ?? "";
    const dbName = srv?.databases.find((d) => d.id === dbid)?.name ?? "database";
    setForm({ name: `Nightly backup ${dbName}`, kind: "backup", server_id: sid, database_id: dbid, cron: "0 2 * * *", enabled: true, keep: "7", options: defaultBackupOpts() });
  };
  useEffect(() => { if (props.dbId && props.visible && !form) newForm(); }, []);
  const save = () => run(async () => {
    if (!form) return;
    const body = { name: form.name, kind: form.kind, server_id: form.server_id, database_id: form.database_id, cron: form.cron, enabled: form.enabled, keep: Number(form.keep) || 0, options: backupPayload(form.options) };
    const s = form.id ? await mapi.updateSchedule(form.id, body) : await mapi.createSchedule(body);
    setForm(null);
    setSelected(s.id);
    await load();
  }, "Schedule saved");
  const srvOf = (id: string) => app.servers.find((s) => s.id === id);
  const dbName = (s: Schedule) => srvOf(s.server_id)?.databases.find((d) => d.id === s.database_id)?.name ?? "(unregistered)";
  const fsrv = form ? srvOf(form.server_id) : undefined;
  return (
    <div className="mt-body">
      <Section wide title="Scheduled jobs" hint="Backups (streamed into the fbadmin archive with retention), sweeps and online validations on a cron schedule, run by fbadmin itself. Times are in the fbadmin host's local time; runs missed while fbadmin was stopped are skipped. The server password must be saved."
        right={<><button className="btn small" onClick={load}><IconRefresh size={12} /></button><button className="btn small primary" onClick={newForm}><IconPlus size={12} /> New schedule</button></>}>
        {items.length === 0 ? <div className="muted">No schedules yet.</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>Name</th><th>Kind</th><th>Database</th><th>Schedule</th><th>Keep</th><th>Next run</th><th>Last run</th><th /></tr></thead>
            <tbody>{items.map((s) => (
              <tr key={s.id} className={selected === s.id ? "sel" : ""} onClick={() => setSelected(s.id)} style={{ cursor: "pointer" }}>
                <td>{s.enabled ? "" : "⏸ "}{s.name}</td><td>{s.kind}</td><td>{srvOf(s.server_id)?.name} / {dbName(s)}</td>
                <td className="mono">{s.cron}</td><td className="num">{s.kind === "backup" ? (s.keep || "all") : "—"}</td>
                <td>{s.enabled ? fmtTime(s.next_run) : "disabled"}</td>
                <td>{s.last_run ? <><span className={"pill " + (s.last_status ?? "")}>{s.last_status}</span> {fmtTime(s.last_run)}</> : "—"}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  <button className="btn small" title="Run now" disabled={busy} onClick={() => run(async () => { setJob(await mapi.runSchedule(s.id)); setSelected(s.id); })}><IconPlay size={12} /></button>{" "}
                  <button className="btn small" onClick={() => setForm(toForm(s))}>Edit</button>{" "}
                  <button className="btn small" onClick={() => run(async () => { await mapi.updateSchedule(s.id, { enabled: !s.enabled }); await load(); })}>{s.enabled ? "Disable" : "Enable"}</button>{" "}
                  <button className="btn small danger" onClick={() => run(async () => {
                    if (!confirm(`Delete schedule "${s.name}"?`)) return;
                    const delFiles = s.kind === "backup" && confirm("Also delete its backup files from the archive?");
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
        <Section title={form.id ? "Edit schedule" : "New schedule"}>
          <div className="form mform">
            <label className="l">Name</label>
            <input className="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <label className="l">Job</label>
            <select className="text" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as Schedule["kind"] })}>
              <option value="backup">Backup (gbak, into the fbadmin archive)</option><option value="sweep">Sweep</option><option value="validate">Online validation</option>
            </select>
            <label className="l">Server / database</label>
            <div className="mt-row">
              <select className="text" value={form.server_id} onChange={(e) => setForm({ ...form, server_id: e.target.value, database_id: srvOf(e.target.value)?.databases[0]?.id ?? "" })}>
                {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <select className="text" style={{ flex: 1 }} value={form.database_id} onChange={(e) => setForm({ ...form, database_id: e.target.value })}>
                {fsrv?.databases.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
            </div>
            <label className="l">Cron (min hour dom mon dow)</label>
            <div>
              <div className="mt-row">
                <input className="text mono" style={{ width: 180 }} value={form.cron} onChange={(e) => setForm({ ...form, cron: e.target.value })} />
                <select className="text" value="" onChange={(e) => e.target.value && setForm({ ...form, cron: e.target.value })}>
                  <option value="">presets…</option>
                  {PRESETS.map(([c, l]) => <option key={c} value={c}>{l} ({c})</option>)}
                </select>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {typeof preview === "string" ? <span className="err">{preview}</span> : `Next: ${preview.map((p) => p.replace("T", " ")).join(" · ")}`}
              </div>
            </div>
            {form.kind === "backup" && <>
              <label className="l">Keep last N backups</label>
              <input className="text" type="number" min={0} value={form.keep} onChange={(e) => setForm({ ...form, keep: e.target.value })} title="0 keeps all" />
            </>}
            <span />
            <label className="check"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Enabled</label>
          </div>
          {form.kind === "backup" && <><h3>Backup options</h3><BackupOptionsForm value={form.options} onChange={(o) => setForm({ ...form, options: o })} caps={caps} /></>}
          <div className="mt-row" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy || !form.database_id || typeof preview === "string"} onClick={save}>Save</button>
            <button className="btn" onClick={() => setForm(null)}>Cancel</button>
          </div>
        </Section>
      )}
      {selected && items.find((s) => s.id === selected)?.kind === "backup" && (
        <Section title="Backups of this schedule">
          {backups.length === 0 ? <div className="muted">No backups yet.</div> : (
            <div className="grid-wrap"><table className="grid">
              <thead><tr><th>File</th><th>Size</th><th>Created</th><th /></tr></thead>
              <tbody>{backups.map((b) => {
                const sid = items.find((s) => s.id === selected)!.server_id;
                return (
                  <tr key={b.path}><td>{b.name}</td><td className="num">{fmtBytes(b.size)}</td><td>{fmtTime(b.modified)}</td>
                    <td><button className="btn small" onClick={() => browserDownload(mapi.archiveUrl(sid, b.path), b.name)}><IconDownload size={12} /> Download</button></td></tr>
                );
              })}</tbody>
            </table></div>
          )}
        </Section>
      )}
      <Section title={selected ? "History of this schedule" : "History (all schedules)"} right={selected && <button className="btn small" onClick={() => setSelected(null)}>Show all</button>}>
        {history.length === 0 ? <div className="muted">No runs yet.</div> : (
          <div className="grid-wrap" style={{ maxHeight: 320 }}><table className="grid">
            <thead><tr><th>Started</th><th>Schedule</th><th>Kind</th><th>Status</th><th>Duration</th><th>Result</th></tr></thead>
            <tbody>{history.map((h, i) => (
              <tr key={i}>
                <td>{fmtTime(h.started_at)}</td><td>{h.schedule_name}</td><td>{h.kind}</td>
                <td><span className={"pill " + h.status}>{h.status}</span></td>
                <td className="num">{(h.finished_at - h.started_at).toFixed(1)} s</td>
                <td style={{ whiteSpace: "normal" }}>{h.error ? <span className="err">{h.error}</span> : h.file ? `${h.file} · ${fmtBytes(h.size)}${h.removed?.length ? ` · ${h.removed.length} old removed` : ""}` : "ok"}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Section>
    </div>
  );
}

/** Roadmap 2.2: restore drills - the schedule form and the report. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { fmtBytes, fmtTime, mapi, type DrillOptions, type DrillReport, type HistoryItem, type Schedule } from "./api";
import { Section } from "./common";
import type { StorageTarget } from "../storage/api";

export interface DrillForm {
  source: "schedule" | "storage"; backup_schedule_id: string; target_id: string; prefix: string; server_id: string; path: string;
  validate_db: boolean; count_rows: boolean; compare: boolean; max_age_hours: string; max_minutes: string; keep: boolean;
}

export const defaultDrillForm = (): DrillForm => ({
  source: "schedule", backup_schedule_id: "", target_id: "", prefix: "", server_id: "", path: "", validate_db: true,
  count_rows: true, compare: true, max_age_hours: "26", max_minutes: "", keep: false,
});

export function toDrillForm(d: DrillOptions | null | undefined): DrillForm {
  if (!d) return defaultDrillForm();
  return {
    source: d.source, backup_schedule_id: d.backup_schedule_id ?? "", target_id: d.target_id ?? "", prefix: d.prefix ?? "",
    server_id: d.server_id ?? "", path: d.path ?? "", validate_db: d.validate_db, count_rows: d.count_rows, compare: d.compare,
    max_age_hours: d.max_age_hours == null ? "" : String(d.max_age_hours), max_minutes: d.max_minutes == null ? "" : String(d.max_minutes), keep: d.keep,
  };
}

export function drillPayload(f: DrillForm): DrillOptions {
  const num = (s: string) => (s.trim() ? Number(s) : null);
  return {
    source: f.source, backup_schedule_id: f.source === "schedule" ? f.backup_schedule_id || null : null,
    target_id: f.source === "storage" ? f.target_id || null : null, prefix: f.source === "storage" ? f.prefix.trim() : "",
    server_id: f.server_id || null, path: f.path.trim() || null, validate_db: f.validate_db, count_rows: f.count_rows || f.compare,
    compare: f.compare, max_age_hours: num(f.max_age_hours), max_minutes: num(f.max_minutes), keep: f.keep,
  };
}

/** One-line result of a drill run in the history. */
export function drillResult(h: HistoryItem): string {
  if (h.drill_ok === undefined) return "";
  const parts = [h.drill_ok ? t("drill passed") : t("drill failed: {checks}", { checks: (h.failed_checks ?? []).join(", ") })];
  if (h.backup) parts.push(h.backup);
  if (h.age_hours != null) parts.push(t("backup {n} h old", { n: h.age_hours.toFixed(1) }));
  if (h.recovery_seconds != null) parts.push(t("recovered in {n} s", { n: h.recovery_seconds }));
  if (h.rows != null) parts.push(t("{n} rows", { n: h.rows }));
  return parts.join(" · ");
}

export function DrillOptionsForm(props: { value: DrillForm; onChange: (v: DrillForm) => void; schedules: Schedule[]; targets: StorageTarget[]; serverId: string; databaseId: string }) {
  const app = useApp();
  const f = props.value;
  const set = <K extends keyof DrillForm>(k: K, v: DrillForm[K]) => props.onChange({ ...f, [k]: v });
  const backups = props.schedules.filter((s) => s.kind === "backup");
  const own = backups.filter((s) => s.server_id === props.serverId && s.database_id === props.databaseId);
  useEffect(() => {  // default: the backup schedule of this database
    if (f.source === "schedule" && !f.backup_schedule_id && own.length) set("backup_schedule_id", own[0].id);
  }, [props.databaseId, own.length]);
  return (
    <div className="form mform">
      <span className="l">{t("Restore the newest backup")}</span>
      <div className="rs-radios" role="radiogroup" aria-label={t("Restore the newest backup")} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label className="check"><input type="radio" name="drill-src" checked={f.source === "schedule"} onChange={() => set("source", "schedule")} /> {t("of a backup schedule (the fbadmin archive)")}</label>
        <label className="check"><input type="radio" name="drill-src" checked={f.source === "storage"} onChange={() => set("source", "storage")} /> {t("in cloud storage (downloaded, as after losing the fbadmin host)")}</label>
      </div>
      {f.source === "schedule" ? <>
        <label className="l" htmlFor="drill-sched">{t("Backup schedule")}</label>
        <select id="drill-sched" className="text" value={f.backup_schedule_id} onChange={(e) => set("backup_schedule_id", e.target.value)}>
          <option value="">{backups.length ? t("choose…") : t("no backup schedule yet")}</option>
          {backups.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </> : <>
        <label className="l" htmlFor="drill-target">{t("Storage target")}</label>
        <select id="drill-target" className="text" value={f.target_id} onChange={(e) => set("target_id", e.target.value)}>
          <option value="">{props.targets.length ? t("choose…") : t("no target (add one under Backup storage)")}</option>
          {props.targets.map((tg) => <option key={tg.id} value={tg.id}>{tg.name} ({tg.kind})</option>)}
        </select>
        <label className="l" htmlFor="drill-prefix">{t("Folder (key prefix)")}</label>
        <input id="drill-prefix" className="text mono" value={f.prefix} placeholder="fbadmin/nightly" onChange={(e) => set("prefix", e.target.value)} />
      </>}
      <label className="l" htmlFor="drill-server">{t("Restore on")}</label>
      <select id="drill-server" className="text" value={f.server_id} onChange={(e) => set("server_id", e.target.value)}>
        <option value="">{t("the database's own server")}</option>
        {app.servers.filter((s) => s.id !== props.serverId && !s.read_only).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <label className="l" htmlFor="drill-path">{t("Restore into (on that server)")}</label>
      <input id="drill-path" className="text mono" value={f.path} placeholder={t("default: next to the database, fbadmin_drill_<id>.fdb")} onChange={(e) => set("path", e.target.value)} />
      <label className="l" htmlFor="drill-age">{t("Newest backup at most (hours old)")}</label>
      <input id="drill-age" className="text" type="number" min={0} step="any" style={{ width: 100 }} value={f.max_age_hours} placeholder={t("no limit")} onChange={(e) => set("max_age_hours", e.target.value)} />
      <label className="l" htmlFor="drill-min">{t("Recovery at most (minutes)")}</label>
      <input id="drill-min" className="text" type="number" min={0} step="any" style={{ width: 100 }} value={f.max_minutes} placeholder={t("no limit")} onChange={(e) => set("max_minutes", e.target.value)} />
      <span className="l">{t("Checks")}</span>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label className="check"><input type="checkbox" checked={f.validate_db} onChange={(e) => set("validate_db", e.target.checked)} /> {t("Online validation of the restored database")}</label>
        <label className="check"><input type="checkbox" checked={f.count_rows || f.compare} disabled={f.compare} onChange={(e) => set("count_rows", e.target.checked)} /> {t("Read every table (row counts)")}</label>
        <label className="check"><input type="checkbox" checked={f.compare} onChange={(e) => set("compare", e.target.checked)} /> {t("Compare tables and row counts with the live database")}</label>
        <label className="check"><input type="checkbox" checked={f.keep} onChange={(e) => set("keep", e.target.checked)} /> {t("Keep the restored database (registered in fbadmin) instead of dropping it")}</label>
      </div>
    </div>
  );
}

const STATUS_CLASS: Record<string, string> = { same: "", differs: "muted", missing: "warn", extra: "warn", error: "err" };

export function DrillReportView(props: { scheduleId: string; refreshKey: number }) {
  const [r, setR] = useState<DrillReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    mapi.drillReport(props.scheduleId).then((x) => { setR(x); setError(null); }).catch((e) => setError(errMsg(e)));
  }, [props.scheduleId, props.refreshKey]);
  if (error) return <Section title={t("Drill report")}><div className="muted">{error}</div></Section>;
  if (!r) return null;
  const src = r.source;
  return (
    <Section wide title={<>{t("Drill report")} <span className={"pill " + (r.ok ? "finished" : "failed")}>{r.ok ? t("passed") : t("failed")}</span></>}
      hint={t("{database}, restored on {server}, {time}", { database: r.database, server: r.server, time: fmtTime(r.finished_at) })}>
      {src && <dl className="k8s-facts drill-facts">
        <dt>{t("Backup")}</dt><dd className="mono">{src.kind === "storage" ? `${src.target}: ${src.key}` : src.name}</dd>
        <dt>{t("Size")}</dt><dd>{fmtBytes(src.size)}</dd>
        <dt>{t("Made")}</dt><dd>{fmtTime(src.modified)} ({t("{n} h old", { n: src.age_hours.toFixed(1) })})</dd>
        <dt>{t("Encrypted")}</dt><dd>{src.encrypted ? t("yes, key {id}", { id: src.key_id ?? "" }) : t("no")}</dd>
        {src.download_seconds != null && <><dt>{t("Download")}</dt><dd>{src.download_seconds} s</dd></>}
        {r.restore?.seconds != null && <><dt>{t("Restore")}</dt><dd>{r.restore.seconds} s</dd></>}
        {r.recovery_seconds != null && <><dt>{t("Recovery time")}</dt><dd>{r.recovery_seconds} s</dd></>}
        {r.restored && <><dt>{t("Restored")}</dt><dd>{t("{tables} tables, {views} views, {procedures} procedures, {triggers} triggers", r.restored)}</dd></>}
        {r.rows != null && <><dt>{t("Rows")}</dt><dd>{r.rows}</dd></>}
      </dl>}
      <ul className="drill-checks">{r.checks.map((c, i) => (
        <li key={i} className={c.ok ? "ok" : c.ok === null ? "warn" : "err"}><span aria-hidden="true">{c.ok ? "✓" : c.ok === null ? "▲" : "✕"}</span> {c.message}</li>
      ))}</ul>
      {r.compare && r.compare.tables.length > 0 && (
        <div className="grid-wrap" style={{ maxHeight: 280 }}><table className="grid">
          <thead><tr><th>{t("Table")}</th><th>{t("Rows in the backup")}</th><th>{t("Rows in the live database")}</th><th>{t("Status")}</th></tr></thead>
          <tbody>{r.compare.tables.map((x) => (
            <tr key={x.table}><td className="mono">{x.table}</td><td className="num">{x.backup_rows ?? "—"}</td><td className="num">{x.live_rows ?? "—"}</td>
              <td className={STATUS_CLASS[x.status] ?? ""}>{t(x.status)}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </Section>
  );
}

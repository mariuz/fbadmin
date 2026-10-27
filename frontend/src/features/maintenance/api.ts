import { ApiError, request, type Body } from "../../api";
import { t } from "../../i18n";
import type { Job } from "../../types";

const srv = (sid: string) => `/api/servers/${sid}`;
const mdb = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/maintenance`;

export interface Capabilities {
  server_version: string;
  engine_major: number;
  features: Record<string, boolean>;
}

export interface Header {
  page_size?: number;
  ods_version?: number;
  database_dialect?: number;
  page_buffers?: number;
  sweep_interval?: number;
  attributes: string[];
  shutdown_mode: "online" | "multi" | "single" | "full";
  forced_writes: boolean;
  read_only: boolean;
  reserve_space: boolean;
  backup_state: string;
  encrypted: boolean;
  [k: string]: unknown;
}

export interface DbState {
  header: Header;
  engine_major: number;
  database: Record<string, any> | null;
  error: string | null;
}

export interface ArchiveItem { path: string; folder: string; name: string; size: number; modified: number }
export interface LimboTx { id: number; multi: boolean; state: string | null; advise: string | null; host_site: string | null; remote_site: string | null; db_path: string | null }

export interface GstatIndex { name: string; id: number; depth?: number; leaf_buckets?: number; nodes?: number; average_key_length?: number; total_dup?: number; max_dup?: number; clustering_factor?: number; fill_distribution: number[]; [k: string]: any }
export interface GstatTable { name: string; id: number; total_records?: number; total_versions?: number; max_versions?: number; total_fragments?: number; data_pages?: number; average_fill?: number; table_size?: number; version_ratio: number; fill_distribution: number[]; indices: GstatIndex[]; [k: string]: any }
export interface Finding { severity: "critical" | "warning" | "info"; table: string | null; index: string | null; issue: string; detail: string; advice: string }
export interface GstatResult { status?: string; header: Record<string, any>; tables: GstatTable[]; attention: Finding[]; text: string; error?: string; database?: string }

export interface TraceSummary {
  id: string; server_id: string; name: string; template: string; database: string | null; session_id: number | null;
  status: string; error: string | null; started_at: number; stopped_at: number | null; event_count: number; line_count: number; config: string;
}
export interface TraceEvent { seq: number; time: string; pid: number; type: string; attachment: number | null; user: string | null; duration_ms: number | null; sql: string | null; plan: string | null; text: string }
export interface TraceTemplate { id: string; label: string; description: string; params: Record<string, number> }

export interface Schedule {
  id: string; name: string; kind: "backup" | "sweep" | "validate" | "schema_snapshot" | "schema_drift"; server_id: string; database_id: string; cron: string;
  enabled: boolean; keep: number; options: Record<string, any>; created_at: number; last_run: number | null;
  last_status: string | null; next_run: number | null;
  verify: boolean; verify_path: string | null; upload: { target_id: string; prefix: string; keep: number } | null;
  drift: DriftOptions | null;
}
export interface DriftOptions {
  baseline: "history" | "database"; history_server_id: string | null; history_database_id: string | null; ref: string | null; fetch: boolean;
  server_id: string | null; database_id: string | null; ignore_types: string[]; ignore_names: string[];
}
export type DriftStatus = "missing" | "extra" | "changed";
export interface DriftItem { schema: string | null; type: string; name: string; status: DriftStatus; baseline_ddl: string | null; database_ddl: string | null }
export interface DriftReport {
  server_id: string; database_id: string; database: string; checked_at: number; duration_ms: number; drift: boolean;
  baseline: { kind: "history" | "database"; label: string; ref?: string; commit?: string; subject?: string; server_id: string; database_id: string };
  summary: Record<DriftStatus, number>; items: DriftItem[]; ignored: number; warnings: string[]; script: string; truncated?: boolean;
  ignore_types: string[]; ignore_names: string[];
}
export interface HistoryItem {
  schedule_id: string; schedule_name: string; kind: string; job_id: string | null; started_at: number; finished_at: number; status: string;
  error: string | null; file?: string; size?: number; removed?: string[]; validation_errors?: number;
  verify?: { ok: boolean; error?: string; seconds?: number; tables?: number };
  upload?: { ok: boolean; error?: string; target?: string; key?: string; removed?: string[] };
  drift?: boolean; summary?: Record<DriftStatus, number>; ignored?: number; baseline?: string;
}

export const mapi = {
  capabilities: (sid: string) => request<Capabilities>("GET", `${srv(sid)}/maintenance/capabilities`),
  prepareDownload: (sid: string, dbid: string, opts: Body<"/api/servers/{server_id}/databases/{db_id}/maintenance/backup/stream", "post">) =>
    request<{ token: string; filename: string; url: string }>("POST", `${mdb(sid, dbid)}/backup/stream`, opts),
  backupJob: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/maintenance/backup", "post">) => request<Job>("POST", `${mdb(sid, dbid)}/backup`, body),
  restoreJob: (sid: string, body: Body<"/api/servers/{server_id}/maintenance/restore", "post">) => request<Job>("POST", `${srv(sid)}/maintenance/restore`, body),
  archive: (sid: string) => request<ArchiveItem[]>("GET", `${srv(sid)}/maintenance/archive`),
  archiveUrl: (sid: string, path: string) => `${srv(sid)}/maintenance/archive/download?path=${encodeURIComponent(path)}`,
  deleteArchive: (sid: string, path: string) => request<void>("DELETE", `${srv(sid)}/maintenance/archive?path=${encodeURIComponent(path)}`),

  nbackup: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/maintenance/nbackup", "post">) => request<Job>("POST", `${mdb(sid, dbid)}/nbackup`, body),
  nrestore: (sid: string, body: Body<"/api/servers/{server_id}/maintenance/nrestore", "post">) => request<Job>("POST", `${srv(sid)}/maintenance/nrestore`, body),
  nbackupLock: (sid: string, dbid: string) => request<DbState>("POST", `${mdb(sid, dbid)}/nbackup/lock`),
  nbackupUnlock: (sid: string, dbid: string) => request<DbState>("POST", `${mdb(sid, dbid)}/nbackup/unlock`),
  nbackupFixup: (sid: string, dbid: string, database_path?: string) =>
    request<{ ok: boolean; output: string[] }>("POST", `${mdb(sid, dbid)}/nbackup/fixup`, { database_path: database_path || null }),
  nbackupHistory: (sid: string, dbid: string) => request<Array<Record<string, any>>>("GET", `${mdb(sid, dbid)}/nbackup/history`),

  validate: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/maintenance/validate", "post">) => request<Job>("POST", `${mdb(sid, dbid)}/validate`, body),
  sweep: (sid: string, dbid: string, parallel_workers?: number | null) =>
    request<Job>("POST", `${mdb(sid, dbid)}/sweep`, { parallel_workers: parallel_workers || null }),
  icu: (sid: string, dbid: string) => request<Job>("POST", `${mdb(sid, dbid)}/icu`),
  limbo: (sid: string, dbid: string) => request<LimboTx[]>("GET", `${mdb(sid, dbid)}/limbo`),
  resolveLimbo: (sid: string, dbid: string, id: number, action: "commit" | "rollback" | "recover") =>
    request<{ output: string[]; limbo: LimboTx[] }>("POST", `${mdb(sid, dbid)}/limbo/${id}/${action}`),

  shutdown: (sid: string, dbid: string, body: { mode: string; method: string; timeout: number }) =>
    request<Header>("POST", `${mdb(sid, dbid)}/shutdown`, body),
  online: (sid: string, dbid: string, mode: string) => request<Header>("POST", `${mdb(sid, dbid)}/online`, { mode }),
  properties: (sid: string, dbid: string) => request<DbState>("GET", `${mdb(sid, dbid)}/properties`),
  setProperties: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/maintenance/properties", "put">) =>
    request<{ changed: string[]; state: DbState }>("PUT", `${mdb(sid, dbid)}/properties`, body),

  gstat: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/maintenance/gstat", "post">) => request<Job>("POST", `${mdb(sid, dbid)}/gstat`, body),
  gstatResult: (sid: string, jobId: string) => request<GstatResult>("GET", `${srv(sid)}/maintenance/gstat/${jobId}`),
  gstatParse: (sid: string, text: string) => request<GstatResult>("POST", `${srv(sid)}/maintenance/gstat/parse`, { text }),

  encrypt: (sid: string, dbid: string, plugin: string, key_name: string) =>
    request<Record<string, any>>("POST", `${mdb(sid, dbid)}/encrypt`, { plugin, key_name: key_name || null }),
  decrypt: (sid: string, dbid: string) => request<Record<string, any>>("POST", `${mdb(sid, dbid)}/decrypt`),
  cryptState: (sid: string, dbid: string) => request<Record<string, any>>("GET", `${mdb(sid, dbid)}/crypt-state`),
  config: (sid: string, dbid: string) =>
    request<{ available: boolean; message: string | null; rows: Array<{ id: number; name: string; value: string | null; default: string | null; is_set: boolean; source: string | null }> }>("GET", `${mdb(sid, dbid)}/config`),

  traceTemplates: (sid: string) => request<TraceTemplate[]>("GET", `${srv(sid)}/trace/templates`),
  tracePreview: (sid: string, body: Body<"/api/servers/{server_id}/trace/preview", "post">) => request<{ config: string }>("POST", `${srv(sid)}/trace/preview`, body),
  traceSessions: (sid: string) => request<{ local: TraceSummary[]; server: Array<{ id: number; name: string | null; user: string | null; date: string | null; flags: string[] }> }>("GET", `${srv(sid)}/trace/sessions`),
  traceStart: (sid: string, body: Body<"/api/servers/{server_id}/trace/sessions", "post">) => request<TraceSummary>("POST", `${srv(sid)}/trace/sessions`, body),
  traceEvents: (sid: string, id: string, after: number) =>
    request<{ session: TraceSummary; events: TraceEvent[]; last_seq: number }>("GET", `${srv(sid)}/trace/sessions/${id}/events?after=${after}&limit=2000`),
  traceStop: (sid: string, id: string) => request<TraceSummary>("POST", `${srv(sid)}/trace/sessions/${id}/stop`),
  traceDelete: (sid: string, id: string) => request<void>("DELETE", `${srv(sid)}/trace/sessions/${id}`),
  traceDownloadUrl: (sid: string, id: string) => `${srv(sid)}/trace/sessions/${id}/download`,
  traceStopServer: (sid: string, sessionId: number) => request("POST", `${srv(sid)}/trace/server-sessions/${sessionId}/stop`),

  schedules: () => request<Schedule[]>("GET", "/api/schedules"),
  createSchedule: (body: Body<"/api/schedules", "post">) => request<Schedule>("POST", "/api/schedules", body),
  updateSchedule: (id: string, body: Body<"/api/schedules/{schedule_id}", "put">) => request<Schedule>("PUT", `/api/schedules/${id}`, body),
  deleteSchedule: (id: string, deleteBackups: boolean) => request<void>("DELETE", `/api/schedules/${id}?delete_backups=${deleteBackups}`),
  runSchedule: (id: string) => request<Job>("POST", `/api/schedules/${id}/run`),
  scheduleHistory: (id?: string) => request<HistoryItem[]>("GET", id ? `/api/schedules/${id}/history` : "/api/schedules/history"),
  scheduleBackups: (id: string) => request<ArchiveItem[]>("GET", `/api/schedules/${id}/backups`),
  verifyBackup: (id: string, path: string) => request<Job>("POST", `/api/schedules/${id}/backups/verify`, { path }),
  driftReport: (id: string) => request<DriftReport>("GET", `/api/schedules/${id}/drift`),
  cronPreview: (expr: string) => request<{ next: string[] }>("GET", `/api/schedules/cron-preview?expr=${encodeURIComponent(expr)}`),
};

/** Start a browser download of a URL without leaving the page. */
export function browserDownload(url: string, filename?: string) {
  const a = document.createElement("a");
  a.href = url;
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Upload a file as the raw request body (streamed by the browser) with progress. */
export function uploadRestore(
  sid: string, file: File, params: Record<string, string | number | boolean | null | undefined>,
  onProgress: (fraction: number) => void,
): Promise<Job> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "" && v !== false) q.set(k, String(v));
  q.set("filename", file.name);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${srv(sid)}/maintenance/restore/upload?${q}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let data: any = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as Job);
      else reject(new ApiError(xhr.status, typeof data.detail === "string" ? data.detail : xhr.statusText, data.code));
    };
    xhr.onerror = () => reject(new Error(t("Upload failed")));
    xhr.send(file);
  });
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export const fmtTime = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleString() : "—");

/** Poll a job until it is no longer running. */
export async function waitJob(id: string, onTick?: (j: Job) => void): Promise<Job> {
  for (;;) {
    const j = await request<Job>("GET", `/api/jobs/${id}`);
    onTick?.(j);
    if (j.status !== "running") return j;
    await new Promise((r) => setTimeout(r, 700));
  }
}

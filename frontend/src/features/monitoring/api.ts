import { request } from "../../api";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/monitoring`;
const qs = (p: Record<string, string | number | boolean | undefined>) => {
  const e = Object.entries(p).filter(([, v]) => v !== undefined && v !== "");
  return e.length ? "?" + e.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&") : "";
};

export type Snapshot = Record<string, number | null> & { ts: number };
export type Rates = Record<string, number | null>;
export type Sample = Snapshot & { rates: Rates };

export interface Features {
  engine_version: string;
  engine_major: number;
  table_stats: boolean;
  compiled_statements: boolean;
  profiler: boolean;
  replication: boolean;
  config: boolean;
}

export interface MonSettings {
  interval: number;
  retention_hours: number;
  slow_seconds: number;
  metrics_enabled: boolean;
}

export interface Tracking { server_id: string; db_id: string; last_sample: number | null; last_error: string | null }

export const monApi = {
  features: (sid: string, dbid: string) => request<Features>("GET", base(sid, dbid) + "/features"),
  snapshot: (sid: string, dbid: string) => request<Snapshot>("GET", base(sid, dbid) + "/snapshot"),
  tables: (sid: string, dbid: string, p: { order_by?: string; limit?: number; include_system?: boolean }) =>
    request<{ available: boolean; reason?: string; tables: Record<string, any>[] }>("GET", base(sid, dbid) + "/tables" + qs(p)),
  compiled: (sid: string, dbid: string, search?: string) =>
    request<{ available: boolean; reason?: string; statements: Record<string, any>[] }>("GET", base(sid, dbid) + "/compiled" + qs({ search })),
  transactions: (sid: string, dbid: string, p: { long_running?: number; gap_warn?: number } = {}) =>
    request<Record<string, any>>("GET", base(sid, dbid) + "/transactions" + qs(p)),
  locks: (sid: string, dbid: string) => request<Record<string, any>>("GET", base(sid, dbid) + "/locks"),
  history: (sid: string, dbid: string, hours: number) =>
    request<{ tracking: Tracking | null; settings: MonSettings; samples: Sample[] }>("GET", base(sid, dbid) + "/history" + qs({ hours })),
  setTracking: (sid: string, dbid: string, enabled: boolean) =>
    request<{ tracking: Tracking | null }>("PUT", base(sid, dbid) + "/history/tracking", { enabled }),
  sampleNow: (sid: string, dbid: string) => request<Snapshot>("POST", base(sid, dbid) + "/history/sample"),
  clearHistory: (sid: string, dbid: string) => request("DELETE", base(sid, dbid) + "/history"),
  slow: (sid: string, dbid: string) => request<{ statements: Record<string, any>[] }>("GET", base(sid, dbid) + "/history/slow"),
  settings: () => request<{ settings: MonSettings; tracked: Tracking[] }>("GET", "/api/monitoring/settings"),
  saveSettings: (s: Partial<MonSettings>) => request<{ settings: MonSettings; tracked: Tracking[] }>("PUT", "/api/monitoring/settings", s),

  profiler: (sid: string, dbid: string) => request<Record<string, any>>("GET", base(sid, dbid) + "/profiler"),
  profileRun: (sid: string, dbid: string, sql: string, description?: string) =>
    request<{ profile_id: number; results: any[]; error: string | null; elapsed_ms: number }>("POST", base(sid, dbid) + "/profiler/run", { sql, description }),
  profileStart: (sid: string, dbid: string, attachment_id: number, description?: string) =>
    request<{ profile_id: number }>("POST", base(sid, dbid) + "/profiler/start", { attachment_id, description }),
  profileFinish: (sid: string, dbid: string, attachment_id: number) =>
    request("POST", base(sid, dbid) + "/profiler/finish", { attachment_id }),
  profileReport: (sid: string, dbid: string, id: number) => request<Record<string, any>>("GET", base(sid, dbid) + `/profiler/${id}`),
  profileDelete: (sid: string, dbid: string, id?: number) =>
    request("DELETE", base(sid, dbid) + "/profiler" + (id !== undefined ? `/${id}` : "")),

  advisor: (sid: string, dbid: string, body: { sources: string[]; sql?: string; min_rows?: number }) =>
    request<Record<string, any>>("POST", base(sid, dbid) + "/advisor", body),
};

// ---------------------------------------------------------------- formatting
export function fmtNum(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "G";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtBytes(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let x = v;
  while (Math.abs(x) >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtNs(ns: number | null | undefined): string {
  if (ns === null || ns === undefined) return "–";
  if (ns >= 1e9) return (ns / 1e9).toFixed(2) + " s";
  if (ns >= 1e6) return (ns / 1e6).toFixed(2) + " ms";
  if (ns >= 1e3) return (ns / 1e3).toFixed(1) + " µs";
  return ns + " ns";
}

export function fmtAge(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "–";
  const s = Math.floor(sec);
  if (s < 120) return `${s} s`;
  if (s < 7200) return `${Math.floor(s / 60)} min`;
  if (s < 172800) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

export const fmtTs = (v: string | null | undefined) => (v ? String(v).replace("T", " ").slice(0, 19) : "–");

import { request } from "../../api";
import type { Job } from "../../types";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/monitoring/checkup`;

export type Severity = "critical" | "warning" | "info";
export interface CheckupAction { kind: string; label: string; confirm: string | null; params: Record<string, any> }
export interface CheckupFinding {
  code: string; severity: Severity; rank: number; title: string; advice: string; params: Record<string, string | number>;
  evidence: { label: string; value?: string; sql?: string }[]; actions: CheckupAction[];
}
export interface CheckupReport {
  database: string; generated_at: number; duration: number; gstat: boolean; engine_version: string | null;
  summary: Record<Severity, number>;
  facts: {
    size_bytes: number | null; pages: number | null; page_size: number | null; page_buffers: number | null; cache_bytes: number | null;
    cache_miss: number | null; sweep_interval: number | null; server_mode: string | null; next_oat: number | null;
    oat_oit: number | null; indices: number; tracked: boolean | null;
  };
  checks: { name: string; status: "done" | "error"; error?: string }[];
  findings: CheckupFinding[];
}
export interface CheckupState { report: CheckupReport | null; job: { id: string; status: string; error: string | null; log: string[] } | null }

export const checkupApi = {
  last: (sid: string, dbid: string) => request<CheckupState>("GET", base(sid, dbid)),
  run: (sid: string, dbid: string, gstat: boolean) => request<Job>("POST", base(sid, dbid), { gstat }),
  recompute: (sid: string, dbid: string, indices: { name: string; schema: string | null }[]) =>
    request<Job>("POST", base(sid, dbid) + "/statistics", { indices }),
};

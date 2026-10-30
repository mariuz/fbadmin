import { request } from "../../api";
import type { Job } from "../../types";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/migrations`;

export interface Source {
  kind: "git" | "directory"; url: string | null; ref: string; path: string; token: string | null; token_user?: string;
  out_of_order: boolean; copy_mode: "metadata" | "full"; scratch: string | null;
}
export type State = "applied" | "pending" | "changed" | "failed" | "late" | "missing" | "baseline" | "baseline_record" | "above_target";
export interface Item {
  version: string; description: string | null; script: string | null; checksum: string | null; state: State;
  installed_at?: number | null; installed_by?: string | null; execution_ms?: number | null; source_ref?: string | null; error?: string | null;
}
export interface Status {
  items: Item[]; pending: string[]; errors: string[]; warnings: string[]; current: string | null; fingerprint: string;
  source_ref: string | null; scripts: number; dry_run_needed: string | null;
  dry_run: { ok: boolean; finished_at: number; pending: string[]; copy: string; summary: Record<string, number> } | null;
}
export interface ScriptResult { version: string; script: string; ok: boolean; ms: number; statements: number; error?: { message: string; line: number; sql: string } }
export interface Change { schema: string | null; type: string; name: string; change: "created" | "altered" | "dropped"; before: string | null; after: string | null }
export interface DryRun {
  ok: boolean; copy: string; scratch: string; scripts: ScriptResult[]; changes: Change[]; summary: Record<string, number>;
  seconds: number; finished_at: number; pending: string[]; source_ref: string | null; by?: string;
}

export const migApi = {
  status: (sid: string, dbid: string) => request<Status>("GET", base(sid, dbid)),
  source: (sid: string, dbid: string) => request<Source | null>("GET", base(sid, dbid) + "/source"),
  saveSource: (sid: string, dbid: string, s: Partial<Source>) => request<Source>("PUT", base(sid, dbid) + "/source", s),
  deleteSource: (sid: string, dbid: string) => request<void>("DELETE", base(sid, dbid) + "/source"),
  dryRun: (sid: string, dbid: string, copy_mode?: string) => request<Job>("POST", base(sid, dbid) + "/dry-run", { copy_mode: copy_mode ?? null }),
  lastDryRun: (sid: string, dbid: string) => request<DryRun>("GET", base(sid, dbid) + "/dry-run"),
  apply: (sid: string, dbid: string, skip_dry_run = false) => request<Job>("POST", base(sid, dbid) + "/apply", { skip_dry_run }),
  baseline: (sid: string, dbid: string, version: string, description: string) => request<{ version: string }>("POST", base(sid, dbid) + "/baseline", { version, description }),
  repair: (sid: string, dbid: string) => request<{ removed_failed: string[]; accepted_checksums: string[] }>("POST", base(sid, dbid) + "/repair", {}),
};

import { request } from "../../api";
import type { Job } from "../../types";

export type ChangeStatus = "open" | "approved" | "rejected" | "withdrawn" | "applying" | "applied" | "failed";
export interface ScriptItem { name: string; sql: string }
export interface ChangeEvent { at: number; by: string | null; action: string; detail: string }
export interface Approval { by: string | null; at: number; comment: string }
export interface SchemaChange { schema: string | null; type: string; name: string; change: "created" | "altered" | "dropped"; before: string | null; after: string | null }
export interface ChangeDryRun {
  ok: boolean; stale: boolean; finished_at: number; seconds: number; copy: string; by: string | null; pending: string[];
  summary: Record<string, number>; changes: SchemaChange[];
  scripts: { script: string; ok: boolean; statements: number; ms: number; error?: { message: string; line: number; sql: string } }[];
}
export interface ChangeActions { edit: boolean; withdraw: boolean; dry_run: boolean; approve: boolean; reject: boolean; apply: boolean; comment: boolean }
export interface ChangeRequest {
  id: string; number: number; server_id: string; database_id: string; server: string | null; database: string | null;
  title: string; description: string; kind: "sql" | "migrations"; sql?: string; scripts?: ScriptItem[];
  status: ChangeStatus; author: string | null; created_at: number; updated_at: number; revision: number;
  approvals: Approval[]; approvals_needed: number; mine: boolean; can: ChangeActions; approve_blocked: string | null;
  statements: number; dry_run?: ChangeDryRun | null; dry_run_job?: string | null; apply_job?: string | null;
  applied?: { at: number; by: string | null; ok: boolean; error: string | null; results: { sql?: string; script?: string; kind?: string; message?: string; row_count?: number | null; ok?: boolean }[] } | null;
  history?: ChangeEvent[];
}
export interface ChangeIn { server_id: string; database_id: string; title: string; description: string; kind: "sql" | "migrations"; sql?: string; scripts?: ScriptItem[] }

const u = (id: string, rest = "") => `/api/changes/${encodeURIComponent(id)}${rest}`;
export const crApi = {
  list: (database_id: string, status?: string) =>
    request<ChangeRequest[]>("GET", `/api/changes?database_id=${encodeURIComponent(database_id)}${status ? `&status=${status}` : ""}`),
  get: (id: string) => request<ChangeRequest>("GET", u(id)),
  create: (b: ChangeIn) => request<ChangeRequest>("POST", "/api/changes", b),
  edit: (id: string, b: Partial<ChangeIn>) => request<ChangeRequest>("PUT", u(id), b),
  comment: (id: string, text: string) => request<ChangeRequest>("POST", u(id, "/comments"), { text }),
  approve: (id: string, text: string) => request<ChangeRequest>("POST", u(id, "/approve"), { text }),
  reject: (id: string, text: string) => request<ChangeRequest>("POST", u(id, "/reject"), { text }),
  withdraw: (id: string) => request<ChangeRequest>("POST", u(id, "/withdraw")),
  dryRun: (id: string, copy_mode: "metadata" | "full") => request<Job>("POST", u(id, "/dry-run"), { copy_mode }),
  apply: (id: string) => request<Job>("POST", u(id, "/apply")),
};

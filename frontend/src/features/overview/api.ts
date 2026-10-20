import { request } from "../../api";

export type Level = "ok" | "warning" | "critical" | "unknown";
export type Code = "password_not_saved" | "locked" | "timeout" | "unreachable" | null;

export interface Issue {
  code: "oat_gap" | "oit_gap" | "long_transactions" | "nbackup_stalled" | "alert" | "job_failed";
  level: Level; value?: number; age?: number | null; rule?: string; message?: string | null; schedule?: string; kind?: string;
}

export interface OverviewDb {
  id: string; name: string; path: string; status: Level; code: Code; error: string | null; issues: Issue[];
  engine_version?: string; ods?: string | null; attachments?: number | null; transactions?: number | null;
  active_statements?: number | null; next_transaction?: number | null; oit?: number | null; oat?: number | null;
  gaps?: { next_oat: number; oat_oit: number; next_oit: number; next_ost: number };
  long_transactions?: number; oldest_transaction_age?: number | null;
  oat_holder?: { id: number; username: string; remote_address: string | null; remote_process: string | null; age_seconds: number | null } | null;
  page_size?: number | null; pages?: number | null; size_bytes?: number | null; memory_used?: number | null;
  nbackup_state?: string | null; read_only?: boolean; open_since?: number | null;
  nbackup_last?: { at: number; level: number } | null;
  replication?: { replica_mode: string; publication_enabled: boolean; sequence: number | null; published_tables: number; lag_segments?: number } | null;
  alerts: Array<{ rule_id: string; rule: string; event: string; state: "firing" | "error"; message: string | null; value: unknown; checked_at: number }>;
  last_backup: { at: number; schedule: string } | null;
  job_failures: Array<{ schedule_id: string; schedule: string; kind: string; at: number | null; error: string | null }>;
  schedules: number;
}

export interface OverviewServer {
  id: string; name: string; host: string; port: number; group: string; shared: boolean; embedded: boolean; read_only: boolean;
  status: Level; code: Code; error: string | null; server_version: string | null; architecture: string | null;
  server_attachments: number | null; databases: OverviewDb[];
}

export interface Overview {
  generated_at: number; elapsed_ms: number; ttl: number; timeout: number; cached: boolean;
  thresholds: { long_transaction: number; gap_warn: number };
  servers: OverviewServer[];
}

export const oapi = {
  get: (refresh = false) => request<Overview>("GET", `/api/overview${refresh ? "?refresh=true" : ""}`),
};

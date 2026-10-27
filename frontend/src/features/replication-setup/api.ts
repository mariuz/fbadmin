import { request } from "../../api";
import type { Job } from "../../types";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/replication`;

export interface SetupBody {
  replica_server_id: string; replica_path: string; replica_name?: string | null;
  mode: "async" | "sync"; transfer: "shared" | "manual"; tables: string[] | null;
  journal_directory?: string | null; archive_directory?: string | null; source_directory?: string | null;
  archive_timeout?: number | null; copy_directory?: string | null; copy_directory_replica?: string | null;
  sync_address?: string | null; sync_user?: string | null; sync_password?: string | null;
}
export interface PlanCheck { level: "error" | "warning" | "info"; code: string; message: string }
export interface Plan {
  ok: boolean; checks: PlanCheck[]; steps: string[]; settings: SetupBody;
  conf: { primary: string; replica: string | null };
  primary: { engine_version: string; guid: string | null; replication_sequence: number | null; publication_enabled: boolean; path: string };
}
export interface RunResult {
  status: string; error: string | null; replica_server_id: string; replica_path: string; mode: string; transfer: string;
  replica_db_id?: string; copy_file?: string; waiting_for_transfer?: boolean; sequence_at_copy?: number;
}
export interface CheckSent { token: string; sent_at: number; primary_sequence: number | null }
export interface CheckState {
  arrived: boolean; table: boolean; replica_mode: string | null; token: string; seconds: number;
  arrived_after: number | null; primary_sequence: number | null; replica_sequence: number | null;
}
export interface Attachment { id: number; user: string; process: string | null; address: string | null }

export const sapi = {
  status: (sid: string, dbid: string) => request<{ supported: boolean; engine_version: string; database_name?: string; tables?: Array<{ name: string; has_key: boolean; published: boolean }> }>("GET", base(sid, dbid)),
  plan: (sid: string, dbid: string, body: SetupBody) => request<Plan>("POST", base(sid, dbid) + "/setup/plan", body),
  reopen: (sid: string, dbid: string) => request<{ others: Attachment[]; reopened: boolean }>("POST", base(sid, dbid) + "/setup/reopen"),
  run: (sid: string, dbid: string, body: SetupBody) => request<Job>("POST", base(sid, dbid) + "/setup/run", body),
  restore: (sid: string, dbid: string, body: SetupBody & { copy_file: string }) => request<Job>("POST", base(sid, dbid) + "/setup/restore", body),
  result: (sid: string, dbid: string, job: string) => request<RunResult>("GET", base(sid, dbid) + `/setup/runs/${job}`),
  check: (sid: string, dbid: string, peer_server: string, peer_db: string) => request<CheckSent>("POST", base(sid, dbid) + "/check", { peer_server, peer_db }),
  poll: (sid: string, dbid: string, token: string) => request<CheckState>("GET", base(sid, dbid) + `/check/${token}`),
  writeConf: (path: string, database: string, block: string) =>
    request<{ path: string; replaced: boolean; backup: string }>("POST", "/api/replication/conf/write", { path, database, block }),
};

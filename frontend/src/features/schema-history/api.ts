import { request } from "../../api";

const enc = encodeURIComponent;
const hbase = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/schema-history`;

export interface HistoryConfig {
  repo_path: string;
  subdir: string;
  branch: string;
  remote_url: string | null;
  push: boolean;
  token: string | null;
  token_user: string;
  committer_name: string;
  committer_email: string;
  after_ddl: boolean;
  resolved_path?: string;
  last_snapshot_at?: number | null;
  last_commit?: string | null;
  last_error?: string | null;
  last_push_error?: string | null;
}
export interface GitStatus { git_available: boolean; git_version: string | null; error: string | null; root: string; server_mode: boolean }
export interface ConfigResponse { status: GitStatus; config: HistoryConfig | null }
export interface Changes { added: string[]; changed: string[]; dropped: string[] }
export interface Commit {
  sha: string; author: string; author_email: string; time: number; committer: string; subject: string; body: string; changes: Changes;
}
export interface ObjectRef { schema: string | null; kind: string; label: string; name: string | null }
export interface CommitFile { path: string; status: "added" | "changed" | "dropped"; object: ObjectRef | null; diff: string }
export interface CommitDetail { sha: string; parent: string | null; author: string; author_email: string; time: number; message: string; files: CommitFile[] }
export interface SnapshotResult {
  committed: boolean; commit: string | null; changes: Changes; push: { ok: boolean; error?: string } | null; files: number;
}
export interface MigrationResult {
  items: Array<{ type: string; name: string; status: "create" | "alter" | "drop"; schema: string | null }>;
  summary: Record<string, number>;
  warnings: string[];
  script: string;
}

export const historyApi = {
  get: (sid: string, dbid: string) => request<ConfigResponse>("GET", hbase(sid, dbid)),
  save: (sid: string, dbid: string, body: HistoryConfig) => request<ConfigResponse>("PUT", hbase(sid, dbid), body),
  remove: (sid: string, dbid: string) => request<void>("DELETE", hbase(sid, dbid)),
  snapshot: (sid: string, dbid: string) => request<SnapshotResult>("POST", `${hbase(sid, dbid)}/snapshot`),
  push: (sid: string, dbid: string) => request<{ ok: boolean }>("POST", `${hbase(sid, dbid)}/push`),
  log: (sid: string, dbid: string, limit = 100, skip = 0) =>
    request<{ commits: Commit[]; has_more: boolean }>("GET", `${hbase(sid, dbid)}/log?limit=${limit}&skip=${skip}`),
  commit: (sid: string, dbid: string, sha: string) => request<CommitDetail>("GET", `${hbase(sid, dbid)}/commits/${enc(sha)}`),
  file: (sid: string, dbid: string, sha: string, path: string) =>
    request<{ path: string; content: string }>("GET", `${hbase(sid, dbid)}/commits/${enc(sha)}/file?path=${enc(path)}`),
  migration: (sid: string, dbid: string, from: string, to: string) =>
    request<MigrationResult>("GET", `${hbase(sid, dbid)}/migration?from_sha=${enc(from)}&to_sha=${enc(to)}`),
};

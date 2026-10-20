import { request } from "../../api";
import type { Job } from "../../types";

export interface Endpoint { server_id: string; db_id: string }

export interface TablePlan {
  name: string;
  in_source: boolean;
  in_target: boolean;
  comparable: boolean;
  reason: string | null;
  key: string[];
  key_type: string | null;
  columns: string[];
  source_only: string[];
  target_only: string[];
  computed: string[];
  ignored: string[];
  type_mismatch: Array<{ column: string; source: string; target: string }>;
  warnings: string[];
}

export interface Listing { source_name: string; target_name: string; tables: TablePlan[] }

export interface TableResult {
  name: string;
  key: string[];
  key_type: string;
  status: "pending" | "running" | "done" | "error";
  error: string | null;
  warnings: string[];
  rows_source: number;
  rows_target: number;
  only_source: number;
  only_target: number;
  different: number;
  identical: number;
  null_keys: number;
  changed_columns: Record<string, number>;
  ordering: "server" | "python" | null;
  columns: string[];
  source_only: string[];
  target_only: string[];
  type_mismatch: Array<{ column: string; source: string; target: string }>;
  ignored: string[];
}

export interface CompareResult {
  status: "running" | "finished" | "failed";
  error: string | null;
  job_id: string;
  source_name: string;
  target_name: string;
  tables: TableResult[];
  skipped: Array<{ name: string; reason: string }>;
  totals: { only_source?: number; only_target?: number; different?: number; identical?: number };
  statements: number;
  script_bytes: number;
  applied: boolean;
  directions: string[];
}

export interface SampleRow { source: unknown[] | null; target: unknown[] | null; changed: string[] }
export interface Samples {
  table: string;
  columns: string[];
  key: string[];
  insert: SampleRow[];
  update: SampleRow[];
  delete: SampleRow[];
}

export interface CompareRequest {
  source: Endpoint;
  target: Endpoint;
  tables: Array<{ name: string; where?: string | null; ignore_columns?: string[] }>;
  ignore_columns: string[];
  inserts: boolean;
  updates: boolean;
  deletes: boolean;
  max_differences: number;
}

const enc = encodeURIComponent;

export const dcApi = {
  tables: (source: Endpoint, target: Endpoint, ignore_columns: string[] = []) =>
    request<Listing>("POST", "/api/datacompare/tables", { source, target, ignore_columns }),
  compare: (body: CompareRequest) => request<Job>("POST", "/api/datacompare/compare", body),
  result: (jobId: string) => request<CompareResult>("GET", `/api/datacompare/jobs/${jobId}`),
  samples: (jobId: string, table: string) =>
    request<Samples>("GET", `/api/datacompare/jobs/${jobId}/samples?table=${enc(table)}`),
  scriptUrl: (jobId: string) => `/api/datacompare/jobs/${jobId}/script`,
  apply: (target: Endpoint, jobId: string) =>
    request<Job>("POST", `/api/servers/${target.server_id}/databases/${target.db_id}/datacompare/${jobId}/apply`,
      { confirm: true }),
};

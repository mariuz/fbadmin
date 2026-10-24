import { request, type Body } from "../../api";

export type RoutineType = "procedures" | "functions" | "block";

export interface DebugVar { name: string; type: string; kind: string; mode?: string }

export interface RoutineInfo {
  otype: RoutineType; name: string | null; kind: "selectable" | "executable" | "function";
  inputs: DebugVar[]; outputs: DebugVar[]; returns: string | null; source: string | null;
  external: boolean; traceable: boolean; reason: string | null; error_line?: number | null;
  locals: DebugVar[]; warnings: string[]; sql_security: string | null;
  profiler?: boolean; engine_version?: string;
}

export type StepKind = "entry" | "step" | "row" | "end" | "handler" | "fetch";

export interface TraceStep {
  k: StepKind; line: number; v: unknown[]; changed?: number[]; row?: number;
  caught?: { gdscode: number | null; sqlstate: string | null; message: string | null };
}

export interface TraceResult {
  compiled: boolean; variables: DebugVar[]; steps: TraceStep[]; stopped: boolean; max_steps?: number;
  columns: string[]; rows: unknown[][]; result: unknown;
  error: { message: string; line: number | null; last_line?: number | null } | null;
  warnings: string[]; instrumented_sql: string; elapsed_ms: number; rolled_back: boolean;
}

export interface CallResult { sql: string; columns: string[]; rows: unknown[][]; error: string | null; truncated: boolean; elapsed_ms: number }

export interface ProfileLine { line: number; count: number; total_ns: number; max_ns: number }
export interface ProfileResult {
  available: boolean; reason?: string; profile_id?: number; line_offset?: number; lines?: ProfileLine[];
  total_ns?: number; run?: CallResult;
}

export interface TestCase {
  id: string; server_id: string; database_id: string; otype: "procedures" | "functions"; routine: string; name: string;
  inputs: Record<string, string | null>; expect: { rows?: unknown[][] | null; error?: string | null };
  created_at: number; updated_at: number; last_status: string | null; last_run_at: number | null;
}

export interface TestResult {
  id: string; name: string; otype: string; routine: string; status: "pass" | "fail" | "error"; diffs: string[];
  run: CallResult | null; elapsed_ms: number;
}

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/debug`;
type RunBody = Body<"/api/servers/{server_id}/databases/{db_id}/debug/trace", "post">;

export const dapi = {
  routine: (sid: string, dbid: string, otype: string, name: string) =>
    request<RoutineInfo>("GET", `${base(sid, dbid)}/routine?otype=${otype}&name=${encodeURIComponent(name)}`),
  parse: (sid: string, dbid: string, sql: string) => request<RoutineInfo>("POST", `${base(sid, dbid)}/parse`, { sql }),
  trace: (sid: string, dbid: string, body: RunBody) => request<TraceResult>("POST", `${base(sid, dbid)}/trace`, body),
  run: (sid: string, dbid: string, body: RunBody) => request<CallResult>("POST", `${base(sid, dbid)}/run`, body),
  profile: (sid: string, dbid: string, body: RunBody) => request<ProfileResult>("POST", `${base(sid, dbid)}/profile`, body),
  /** Cancel a run started with this run_id (its request then fails with code "cancelled"). */
  cancel: (sid: string, dbid: string, runId: string) => request<{ cancelled: boolean }>("POST", `${base(sid, dbid)}/cancel`, { run_id: runId }),
  tests: (sid: string, dbid: string, otype?: string, routine?: string) => {
    const q = new URLSearchParams();
    if (otype) q.set("otype", otype);
    if (routine) q.set("routine", routine);
    return request<TestCase[]>("GET", `${base(sid, dbid)}/tests?${q}`);
  },
  addTest: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/debug/tests", "post">) =>
    request<TestCase>("POST", `${base(sid, dbid)}/tests`, body),
  updateTest: (sid: string, dbid: string, id: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/debug/tests/{test_id}", "put">) =>
    request<TestCase>("PUT", `${base(sid, dbid)}/tests/${id}`, body),
  deleteTest: (sid: string, dbid: string, id: string) => request<void>("DELETE", `${base(sid, dbid)}/tests/${id}`),
  runTests: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/debug/tests/run", "post">) =>
    request<{ results: TestResult[]; summary: Record<string, number>; rolled_back: boolean }>("POST", `${base(sid, dbid)}/tests/run`, body),
};

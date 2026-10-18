import type { Completion, Job, ObjectDetail, ScriptResult, Server, SessionOptions } from "./types";

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** Called when the backend needs a password; returns false if the user cancelled. */
let passwordHandler: ((serverId: string) => Promise<boolean>) | null = null;
export function setPasswordHandler(fn: (serverId: string) => Promise<boolean>) {
  passwordHandler = fn;
}

function serverIdFromPath(path: string): string | null {
  const m = path.match(/^\/api\/servers\/([^/]+)/);
  return m ? m[1] : null;
}

export async function request<T>(method: string, path: string, body?: unknown, retry = true): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && data.code === "password_required" && retry && passwordHandler) {
      const sid = serverIdFromPath(path) ?? (body as any)?.server_id;
      if (sid && (await passwordHandler(sid))) return request<T>(method, path, body, false);
    }
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    throw new ApiError(res.status, detail || res.statusText, data.code);
  }
  return data as T;
}

const enc = encodeURIComponent;
const db = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}`;

export const api = {
  servers: () => request<Server[]>("GET", "/api/servers"),
  addServer: (s: Record<string, unknown>) => request<Server>("POST", "/api/servers", s),
  updateServer: (id: string, s: Record<string, unknown>) => request<Server>("PUT", `/api/servers/${id}`, s),
  deleteServer: (id: string) => request<void>("DELETE", `/api/servers/${id}`),
  connectServer: (id: string, password?: string) =>
    request<{ server_version: string }>("POST", `/api/servers/${id}/connect`, { password }),
  disconnectServer: (id: string) => request("POST", `/api/servers/${id}/disconnect`),
  serverInfo: (id: string) => request<Record<string, any>>("GET", `/api/servers/${id}/info`),
  serverLog: (id: string) => request<{ log: string }>("GET", `/api/servers/${id}/log`),

  addDatabase: (sid: string, d: { name: string; path: string }) => request("POST", `/api/servers/${sid}/databases`, d),
  createDatabase: (sid: string, d: Record<string, unknown>) =>
    request("POST", `/api/servers/${sid}/databases/create`, d),
  updateDatabase: (sid: string, dbid: string, d: { name: string; path: string }) =>
    request("PUT", db(sid, dbid), d),
  removeDatabase: (sid: string, dbid: string) => request<void>("DELETE", db(sid, dbid)),
  dropDatabase: (sid: string, dbid: string) => request<void>("POST", db(sid, dbid) + "/drop"),
  connectDatabase: (sid: string, dbid: string) =>
    request<{ engine_version: string; charset: string }>("POST", db(sid, dbid) + "/connect"),
  disconnectDatabase: (sid: string, dbid: string) => request("POST", db(sid, dbid) + "/disconnect"),
  databaseInfo: (sid: string, dbid: string) =>
    request<{ info: Record<string, any>; counts: Record<string, number> }>("GET", db(sid, dbid) + "/info"),

  objects: (sid: string, dbid: string, otype: string) =>
    request<Array<Record<string, any>>>("GET", db(sid, dbid) + `/objects/${otype}`),
  object: (sid: string, dbid: string, otype: string, name: string) =>
    request<ObjectDetail>("GET", db(sid, dbid) + `/objects/${otype}/${enc(name)}`),
  script: (sid: string, dbid: string, otype: string, name: string, kind: string) =>
    request<{ sql: string }>("GET", db(sid, dbid) + `/objects/${otype}/${enc(name)}/script?kind=${kind}`),
  dropObject: (sid: string, dbid: string, otype: string, name: string) =>
    request("DELETE", db(sid, dbid) + `/objects/${otype}/${enc(name)}`),
  execute: (sid: string, dbid: string, sql: string) => request<ScriptResult>("POST", db(sid, dbid) + "/execute", { sql }),

  tableData: (sid: string, dbid: string, name: string, params: Record<string, string | number | boolean>) => {
    const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
    return request<{
      sql: string;
      columns: Array<{ name: string; type: string }>;
      column_meta: Array<Record<string, any>>;
      primary_key: string[];
      rows: unknown[][];
      offset: number;
      limit: number;
      has_more: boolean;
    }>("GET", db(sid, dbid) + `/tables/${enc(name)}/data?${q}`);
  },
  tableCount: (sid: string, dbid: string, name: string) =>
    request<{ count: number }>("GET", db(sid, dbid) + `/tables/${enc(name)}/count`),
  saveTableData: (sid: string, dbid: string, name: string, changes: Record<string, unknown>) =>
    request<{ statements: number; affected: number }>("POST", db(sid, dbid) + `/tables/${enc(name)}/data`, changes),

  monitor: (sid: string, dbid: string) => request<Record<string, any>>("GET", db(sid, dbid) + "/monitor"),
  killAttachment: (sid: string, dbid: string, id: number) =>
    request("DELETE", db(sid, dbid) + `/monitor/attachments/${id}`),
  cancelStatement: (sid: string, dbid: string, id: number) =>
    request("DELETE", db(sid, dbid) + `/monitor/statements/${id}`),

  openSession: (server_id: string, database_id: string) =>
    request<{ session_id: string; options: SessionOptions }>("POST", "/api/query/sessions", { server_id, database_id }),
  runQuery: (sess: string, body: {
    sql: string; autocommit: boolean; with_plan?: boolean; with_stats?: boolean; max_rows?: number;
    params?: Record<string, unknown>;
  }) => request<ScriptResult>("POST", `/api/query/sessions/${sess}/execute`, body),
  fetchMore: (sess: string, rows: number) =>
    request<{ rows: unknown[][]; has_more: boolean; transaction_open: boolean }>("POST", `/api/query/sessions/${sess}/fetch`, { rows }),
  cancel: (sess: string) => request<{ cancelled: number }>("POST", `/api/query/sessions/${sess}/cancel`),
  setOptions: (sess: string, o: Partial<SessionOptions>) => request<SessionOptions>("PUT", `/api/query/sessions/${sess}/options`, o),
  queryParams: (sql: string) => request<{ params: string[] }>("POST", "/api/query/params", { sql }),
  explain: (sess: string, sql: string) =>
    request<{ plan: string | null; detailed: string | null }>("POST", `/api/query/sessions/${sess}/explain`, { sql }),
  commit: (sess: string) => request("POST", `/api/query/sessions/${sess}/commit`),
  rollback: (sess: string) => request("POST", `/api/query/sessions/${sess}/rollback`),
  completion: (sid: string, dbid: string) => request<Completion>("GET", db(sid, dbid) + "/completion"),
  closeSession: (sess: string) => request<void>("DELETE", `/api/query/sessions/${sess}`),

  backup: (sid: string, body: Record<string, unknown>) => request<Job>("POST", `/api/servers/${sid}/backup`, body),
  restore: (sid: string, body: Record<string, unknown>) => request<Job>("POST", `/api/servers/${sid}/restore`, body),
  sweep: (sid: string, dbid: string) => request<Job>("POST", db(sid, dbid) + "/sweep"),
  statistics: (sid: string, dbid: string) => request<Job>("POST", db(sid, dbid) + "/statistics"),
  recomputeSelectivity: (sid: string, dbid: string) => request<Job>("POST", db(sid, dbid) + "/recompute-selectivity"),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
  job: (id: string) => request<Job>("GET", `/api/jobs/${id}`),
};

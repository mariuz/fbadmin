import type { Completion, Job, ObjectDetail, ScriptResult, Server, SessionOptions } from "./types";
import type { components, paths } from "./api-types.gen";

/** Request bodies, generated from the backend's Pydantic models (scripts/gen-api-types.sh). */
type In = components["schemas"];

/** JSON request body of ``method`` on an OpenAPI ``path`` (also checks at compile time that the path exists),
 *  e.g. ``Body<"/api/schedules", "post">``. */
export type Body<P extends keyof paths, M extends "post" | "put" | "patch"> =
  NonNullable<paths[P][M]> extends { requestBody?: { content: { "application/json": infer B } } } ? B : never;

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

// --- Firebird 6 SQL schemas ------------------------------------------------------------------
// Each database has an "active" schema (picked in the tree, a tab or the Query Tool). Requests to
// that database carry it in the X-Fbadmin-Schema header, so the backend scopes metadata to it.
const activeSchemas = new Map<string, string>();
export function setActiveSchema(sid: string, dbid: string, schema: string | null | undefined) {
  if (schema) activeSchemas.set(`${sid}/${dbid}`, schema);
  else activeSchemas.delete(`${sid}/${dbid}`);
}
export function activeSchemaOf(sid?: string, dbid?: string): string | undefined {
  return sid && dbid ? activeSchemas.get(`${sid}/${dbid}`) : undefined;
}
/** Run ``fn`` (which must start its requests synchronously) with ``schema`` active for that database. */
export function withSchema<T>(sid: string, dbid: string, schema: string | undefined, fn: () => T): T {
  if (!schema) return fn();
  const prev = activeSchemaOf(sid, dbid);
  setActiveSchema(sid, dbid, schema);
  try {
    return fn();
  } finally {
    setActiveSchema(sid, dbid, prev);
  }
}
function schemaHeaders(path: string, body: unknown): Record<string, string> {
  const m = path.match(/^\/api\/servers\/([^/]+)\/databases\/([^/?]+)/);
  const b = (body ?? {}) as { server_id?: string; database_id?: string };
  const schema = m ? activeSchemas.get(`${m[1]}/${m[2]}`) : activeSchemaOf(b.server_id, b.database_id);
  return schema ? { "X-Fbadmin-Schema": schema } : {};
}

// --- security (server mode): CSRF token header and handlers for auth errors -------------
/** Error code (e.g. "auth_required", "master_password_required") -> handler; true = retry the request. */
const errorHandlers: Record<string, () => Promise<boolean>> = {};
export function setErrorHandler(code: string, fn: () => Promise<boolean>) {
  errorHandlers[code] = fn;
}
function csrfHeaders(method: string): Record<string, string> {
  if (method === "GET" || method === "HEAD") return {};
  const m = document.cookie.match(/(?:^|;\s*)fbadmin_csrf=([^;]+)/);
  return m ? { "X-CSRF-Token": decodeURIComponent(m[1]) } : {};
}

export async function request<T>(method: string, path: string, body?: unknown, retry = true): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...csrfHeaders(method), ...schemaHeaders(path, body),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && data.code === "password_required" && retry && passwordHandler) {
      const sid = serverIdFromPath(path) ?? (body as any)?.server_id;
      if (sid && (await passwordHandler(sid))) return request<T>(method, path, body, false);
    }
    if (retry && data.code && errorHandlers[data.code] && (await errorHandlers[data.code]())) {
      return request<T>(method, path, body, false);
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
  addServer: (s: In["ServerIn"]) => request<Server>("POST", "/api/servers", s),
  updateServer: (id: string, s: In["ServerUpdate"]) => request<Server>("PUT", `/api/servers/${id}`, s),
  deleteServer: (id: string) => request<void>("DELETE", `/api/servers/${id}`),
  connectServer: (id: string, password?: string) =>
    request<{ server_version: string }>("POST", `/api/servers/${id}/connect`, { password }),
  disconnectServer: (id: string) => request("POST", `/api/servers/${id}/disconnect`),
  serverInfo: (id: string) => request<Record<string, any>>("GET", `/api/servers/${id}/info`),
  serverLog: (id: string) => request<{ log: string }>("GET", `/api/servers/${id}/log`),

  addDatabase: (sid: string, d: In["DatabaseIn"]) => request("POST", `/api/servers/${sid}/databases`, d),
  createDatabase: (sid: string, d: In["CreateDatabaseIn"]) =>
    request("POST", `/api/servers/${sid}/databases/create`, d),
  updateDatabase: (sid: string, dbid: string, d: In["DatabaseIn"]) =>
    request("PUT", db(sid, dbid), d),
  removeDatabase: (sid: string, dbid: string) => request<void>("DELETE", db(sid, dbid)),
  dropDatabase: (sid: string, dbid: string) => request<void>("POST", db(sid, dbid) + "/drop"),
  connectDatabase: (sid: string, dbid: string) =>
    request<{ engine_version: string; charset: string }>("POST", db(sid, dbid) + "/connect"),
  disconnectDatabase: (sid: string, dbid: string) => request("POST", db(sid, dbid) + "/disconnect"),
  schemas: (sid: string, dbid: string) =>
    request<{ supported: boolean; schemas: Array<{ name: string; owner: string; system_flag: number; description?: string }> }>(
      "GET", db(sid, dbid) + "/schemas"),
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
  saveTableData: (sid: string, dbid: string, name: string, changes: In["DataChanges"]) =>
    request<{ statements: number; affected: number }>("POST", db(sid, dbid) + `/tables/${enc(name)}/data`, changes),

  monitor: (sid: string, dbid: string) => request<Record<string, any>>("GET", db(sid, dbid) + "/monitor"),
  killAttachment: (sid: string, dbid: string, id: number) =>
    request("DELETE", db(sid, dbid) + `/monitor/attachments/${id}`),
  cancelStatement: (sid: string, dbid: string, id: number) =>
    request("DELETE", db(sid, dbid) + `/monitor/statements/${id}`),

  /** ``schema`` (Firebird 6) becomes the search path of the new session. */
  openSession: (server_id: string, database_id: string, schema?: string) =>
    withSchema(server_id, database_id, schema, () =>
      request<{ session_id: string; options: SessionOptions }>("POST", "/api/query/sessions", { server_id, database_id })),
  runQuery: (sess: string, body: In["fbadmin__api__query__ExecuteIn"]) => request<ScriptResult>("POST", `/api/query/sessions/${sess}/execute`, body),
  fetchMore: (sess: string, rows: number) =>
    request<{ rows: unknown[][]; has_more: boolean; transaction_open: boolean }>("POST", `/api/query/sessions/${sess}/fetch`, { rows }),
  cancel: (sess: string) => request<{ cancelled: number }>("POST", `/api/query/sessions/${sess}/cancel`),
  setOptions: (sess: string, o: In["OptionsIn"]) => request<SessionOptions>("PUT", `/api/query/sessions/${sess}/options`, o),
  queryParams: (sql: string) => request<{ params: string[] }>("POST", "/api/query/params", { sql }),
  explain: (sess: string, sql: string) =>
    request<{ plan: string | null; detailed: string | null }>("POST", `/api/query/sessions/${sess}/explain`, { sql }),
  commit: (sess: string) => request("POST", `/api/query/sessions/${sess}/commit`),
  rollback: (sess: string) => request("POST", `/api/query/sessions/${sess}/rollback`),
  completion: (sid: string, dbid: string) => request<Completion>("GET", db(sid, dbid) + "/completion"),
  closeSession: (sess: string) => request<void>("DELETE", `/api/query/sessions/${sess}`),

  backup: (sid: string, body: In["fbadmin__api__servers__BackupIn"]) => request<Job>("POST", `/api/servers/${sid}/backup`, body),
  restore: (sid: string, body: In["fbadmin__api__servers__RestoreIn"]) => request<Job>("POST", `/api/servers/${sid}/restore`, body),
  sweep: (sid: string, dbid: string) => request<Job>("POST", db(sid, dbid) + "/sweep"),
  statistics: (sid: string, dbid: string) => request<Job>("POST", db(sid, dbid) + "/statistics"),
  recomputeSelectivity: (sid: string, dbid: string) => request<Job>("POST", db(sid, dbid) + "/recompute-selectivity"),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
  job: (id: string) => request<Job>("GET", `/api/jobs/${id}`),
  cancelJob: (id: string) => request<Job>("POST", `/api/jobs/${id}/cancel`),
};

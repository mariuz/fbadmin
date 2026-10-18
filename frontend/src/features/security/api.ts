import { request, type Body } from "../../api";
import type { AuthStatus, VaultStatus } from "./state";

const enc = encodeURIComponent;
const sec = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/security`;
const q = (params: Record<string, unknown>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export type SqlResult = { sql: string[]; executed: boolean };

export interface FbUser {
  name: string;
  first_name: string;
  middle_name: string;
  last_name: string;
  active: boolean;
  admin: boolean;
  db_admin: boolean;
  plugin: string | null;
  description: string | null;
  tags: Record<string, string>;
}

export interface Mapping {
  name: string;
  global: boolean;
  using: "plugin" | "any_plugin" | "serverwide" | "mapping" | "all";
  plugin: string | null;
  database: string | null;
  from_type: string;
  from_name: string;
  to_type: "USER" | "ROLE";
  to_name: string | null;
  system: boolean;
  description: string | null;
}

export interface RoleInfo {
  name: string;
  owner: string;
  description: string | null;
  system: boolean;
  privileges: string[];
  members: Array<{ user: string; admin_option: boolean }>;
}

export interface AppUser {
  id: number;
  username: string;
  email: string | null;
  role: "admin" | "user";
  active: boolean;
  auth_source: string;
  created_at: number;
  last_login: number | null;
  has_vault?: boolean;
}

export interface AuditRow {
  id: number;
  ts: number;
  username: string;
  client_ip: string | null;
  server_id: string | null;
  server_name: string | null;
  server_host: string | null;
  db_id: string | null;
  db_name: string | null;
  db_path: string | null;
  category: string;
  statement: string;
  success: boolean;
  error: string | null;
  row_count: number | null;
  elapsed_ms: number | null;
}

type LoginResult = AuthStatus & { csrf_token: string };

export const secApi = {
  login: (username: string, password: string) => request<LoginResult>("POST", "/api/auth/login", { username, password }, false),
  setup: (body: { username: string; email?: string; password: string }) =>
    request<LoginResult>("POST", "/api/auth/setup", body, false),
  logout: () => request("POST", "/api/auth/logout", undefined, false),
  changePassword: (old_password: string, new_password: string) =>
    request("POST", "/api/auth/password", { old_password, new_password }),

  vault: () => request<VaultStatus>("GET", "/api/auth/vault"),
  vaultSetup: (password: string) => request<VaultStatus & { migrated: number }>("POST", "/api/auth/vault/setup", { password }, false),
  vaultUnlock: (password: string) => request<VaultStatus>("POST", "/api/auth/vault/unlock", { password }, false),
  vaultLock: () => request<VaultStatus>("POST", "/api/auth/vault/lock"),
  vaultChange: (old_password: string, new_password: string) =>
    request<VaultStatus>("POST", "/api/auth/vault/change", { old_password, new_password }),
  vaultDisable: (password: string) => request<VaultStatus>("POST", "/api/auth/vault/disable", { password }),
  vaultReset: () => request<VaultStatus & { dropped: number }>("POST", "/api/auth/vault/reset", undefined, false),

  users: () => request<AppUser[]>("GET", "/api/admin/users"),
  createUser: (u: Body<"/api/admin/users", "post">) => request<AppUser>("POST", "/api/admin/users", u),
  updateUser: (id: number, u: Body<"/api/admin/users/{user_id}", "put">) => request<AppUser>("PUT", `/api/admin/users/${id}`, u),
  deleteUser: (id: number) => request<void>("DELETE", `/api/admin/users/${id}`),
  audit: (params: Record<string, unknown>) =>
    request<{ total: number; rows: AuditRow[]; enabled: boolean }>("GET", "/api/admin/audit" + q(params)),

  // Firebird security (SQL, per database)
  fbUsers: (sid: string, dbid: string) =>
    request<{ users: FbUser[]; plugins: string[]; security_database: string | null; current_user: string }>("GET", sec(sid, dbid) + "/users"),
  createFbUser: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/security/users", "post">, preview = false) =>
    request<SqlResult>("POST", sec(sid, dbid) + "/users" + q({ preview }), body),
  alterFbUser: (sid: string, dbid: string, name: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/security/users/{name}", "put">, preview = false) =>
    request<SqlResult>("PUT", sec(sid, dbid) + `/users/${enc(name)}` + q({ preview }), body),
  dropFbUser: (sid: string, dbid: string, name: string, plugin?: string | null) =>
    request<SqlResult>("DELETE", sec(sid, dbid) + `/users/${enc(name)}` + q({ plugin })),

  mappings: (sid: string, dbid: string) =>
    request<{ local: Mapping[]; global: Mapping[]; global_error: string | null }>("GET", sec(sid, dbid) + "/mappings"),
  createMapping: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/security/mappings", "post">, preview = false) =>
    request<SqlResult>("POST", sec(sid, dbid) + "/mappings" + q({ preview }), body),
  alterMapping: (sid: string, dbid: string, name: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/security/mappings/{name}", "put">, preview = false) =>
    request<SqlResult>("PUT", sec(sid, dbid) + `/mappings/${enc(name)}` + q({ preview }), body),
  dropMapping: (sid: string, dbid: string, name: string, isGlobal: boolean) =>
    request<SqlResult>("DELETE", sec(sid, dbid) + `/mappings/${enc(name)}` + q({ is_global: isGlobal })),

  roles: (sid: string, dbid: string) =>
    request<{ supported: boolean; engine_major: number; privileges: string[]; roles: RoleInfo[] }>("GET", sec(sid, dbid) + "/roles"),
  createRole: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/security/roles", "post">, preview = false) =>
    request<SqlResult>("POST", sec(sid, dbid) + "/roles" + q({ preview }), body),
  alterRole: (sid: string, dbid: string, name: string, privileges: string[], preview = false) =>
    request<SqlResult>("PUT", sec(sid, dbid) + `/roles/${enc(name)}` + q({ preview }), { privileges }),
  grantRole: (sid: string, dbid: string, name: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/security/roles/{name}/members", "post">) =>
    request<SqlResult>("POST", sec(sid, dbid) + `/roles/${enc(name)}/members`, body),

  // Services API users (per server)
  svcUsers: (sid: string, securityDb?: string) =>
    request<{ users: Array<{ user_name: string; first_name: string; middle_name: string; last_name: string; admin: boolean }> }>(
      "GET", `/api/servers/${sid}/fbusers` + q({ security_db: securityDb })),
  svcAddUser: (sid: string, body: Body<"/api/servers/{server_id}/fbusers", "post">) => request("POST", `/api/servers/${sid}/fbusers`, body),
  svcModifyUser: (sid: string, name: string, body: Body<"/api/servers/{server_id}/fbusers/{name}", "put">) =>
    request("PUT", `/api/servers/${sid}/fbusers/${enc(name)}`, body),
  svcDeleteUser: (sid: string, name: string, securityDb?: string) =>
    request("DELETE", `/api/servers/${sid}/fbusers/${enc(name)}` + q({ security_db: securityDb })),
};

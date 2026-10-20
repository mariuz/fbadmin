/** API client: two-factor sign-in, API tokens and permissions (server mode). */
import { request } from "../../api";
import type { AuthStatus } from "../security/state";

export type Level = "none" | "read" | "write" | "ddl" | "admin";

export interface Enrolment {
  secret: string;
  otpauth_uri: string;
  issuer: string;
  account: string;
  digits: number;
  period: number;
}

export interface MfaStatus {
  available: boolean;
  enabled: boolean;
  required: boolean;
  required_for_user: boolean;
  required_for_all: boolean;
  pending: boolean;
  recovery_codes_left: number;
  enabled_at: number | null;
}

/** Answer of /api/auth/login when a second factor is needed. */
export type LoginAnswer = AuthStatus & {
  csrf_token?: string;
  mfa_token?: string;
  mfa_required?: boolean;
  mfa_setup_required?: boolean;
  enrol?: Enrolment;
  recovery_codes?: string[];
};

export interface ApiToken {
  id: number;
  user_id: number;
  username?: string;
  name: string;
  hint: string;
  scope: "read-only" | "read-write";
  server_ids: string[] | null;
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
  last_used_ip: string | null;
  revoked_at: number | null;
  status: "active" | "expired" | "revoked";
}

export interface EffectiveDb {
  db_id: string;
  name: string;
  level: Level;
  objects: Array<{ pattern: string; level: Level }>;
}

export interface EffectiveServer {
  server_id: string;
  server_name: string;
  shared: boolean;
  read_only: boolean;
  level: Level;
  databases: EffectiveDb[];
}

export interface Effective {
  restricted: boolean;
  cap: Level;
  token_servers: string[] | null;
  servers: EffectiveServer[];
  levels: Level[];
  level_labels: Record<Level, string>;
  admin?: boolean;
  username?: string;
  token?: string | null;
  server_mode?: boolean;
}

export interface Grant {
  id: number;
  subject_type: "user" | "group";
  subject: string;
  server_id: string;
  db_id: string | null;
  object_pattern: string | null;
  level: Level;
  note: string | null;
  created_at: number;
}

export interface Group {
  name: string;
  description: string | null;
  members: number[];
}

export interface AccessOverview {
  default: "full" | "none";
  grants: Grant[];
  groups: Group[];
  levels: Level[];
  level_labels: Record<Level, string>;
}

export interface MfaOverview {
  require_all: boolean;
  users: Array<{ id: number; username: string; auth_source: string; available: boolean; enabled: boolean; required: boolean }>;
}

const enc = encodeURIComponent;

export const accessApi = {
  loginMfa: (mfa_token: string, code: string) =>
    request<LoginAnswer & { mfa_method: string }>("POST", "/api/auth/login/mfa", { mfa_token, code }, false),
  loginMfaSetup: (mfa_token: string, code: string) =>
    request<LoginAnswer>("POST", "/api/auth/login/mfa-setup", { mfa_token, code }, false),

  mfa: () => request<MfaStatus>("GET", "/api/auth/2fa"),
  mfaSetup: () => request<Enrolment>("POST", "/api/auth/2fa/setup"),
  mfaEnable: (code: string) => request<MfaStatus & { recovery_codes: string[] }>("POST", "/api/auth/2fa/enable", { code }),
  mfaDisable: (code: string) => request<MfaStatus>("POST", "/api/auth/2fa/disable", { code }),
  mfaRecovery: (code: string) => request<MfaStatus & { recovery_codes: string[] }>("POST", "/api/auth/2fa/recovery-codes", { code }),

  me: () => request<Effective>("GET", "/api/access/me"),
  tokens: () => request<ApiToken[]>("GET", "/api/access/me/tokens"),
  createToken: (body: { name: string; scope: string; expires_days: number | null; server_ids: string[] | null }) =>
    request<ApiToken & { token: string; vault: boolean }>("POST", "/api/access/me/tokens", body),
  revokeToken: (id: number) => request<ApiToken>("DELETE", `/api/access/me/tokens/${id}`),

  allTokens: () => request<ApiToken[]>("GET", "/api/admin/tokens"),
  adminRevokeToken: (id: number) => request<ApiToken>("DELETE", `/api/admin/tokens/${id}`),

  mfaOverview: () => request<MfaOverview>("GET", "/api/admin/2fa"),
  mfaPolicy: (require_all: boolean) => request<MfaOverview>("PUT", "/api/admin/2fa", { require_all }),
  mfaRequireUser: (id: number, required: boolean) => request<MfaOverview>("PUT", `/api/admin/users/${id}/2fa`, { required }),
  mfaReset: (id: number) => request<MfaOverview>("DELETE", `/api/admin/users/${id}/2fa`),

  access: () => request<AccessOverview>("GET", "/api/admin/access"),
  accessDefault: (value: "full" | "none") => request<AccessOverview>("PUT", "/api/admin/access/settings", { default: value }),
  addGrant: (g: Omit<Grant, "id" | "created_at">) => request<Grant>("POST", "/api/admin/access/grants", g),
  deleteGrant: (id: number) => request<void>("DELETE", `/api/admin/access/grants/${id}`),
  saveGroup: (name: string, description: string | null, members: number[]) =>
    request<Group>("PUT", `/api/admin/access/groups/${enc(name)}`, { description, members }),
  deleteGroup: (name: string) => request<void>("DELETE", `/api/admin/access/groups/${enc(name)}`),
  effectiveFor: (userId: number) => request<Effective>("GET", `/api/admin/access/effective/${userId}`),
};

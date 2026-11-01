import { request } from "../../api";
import type { Job } from "../../types";

export type Strategy = "keep" | "null" | "fixed" | "hash" | "email" | "name" | "first_name" | "last_name" | "city" | "phone"
  | "words" | "redact" | "number_noise" | "date_shift" | "shuffle";
export interface Rule { strategy: Strategy; value?: unknown; keep?: number; percent?: number; days?: number }
export type Rules = Record<string, Record<string, Rule>>;
export interface Column {
  name: string; data_type: string; kind: string; length: number | null; not_null: boolean; key: boolean; unique: boolean;
  identity: boolean; allowed: Strategy[]; suggested: Rule | null; linked: string[];
}
export interface Schema { tables: { name: string; columns: Column[] }[]; foreign_keys: unknown[]; engine_version: string }
export interface Target { server_id: string; path: string; register_as?: string | null; replace?: boolean }
export interface LastRun {
  at: number; ok: boolean; error?: string; rows?: number; columns?: number; seconds?: number; target?: string; database_id?: string | null;
  foreign_keys?: number; leaks?: { table: string; column: string; rows: number; sampled: number }[];
  tables?: Record<string, { rows: number; columns: Record<string, string> }>;
}
export interface Profile {
  id: string; name: string; server_id: string; database_id: string; rules: Rules; target: Target | null;
  scratch_path: string | null; owner: string | null; updated_at: number; last_run: LastRun | null; rule_count: number;
}
export interface Preview { columns: { name: string; type: string; strategy: Strategy }[]; original: unknown[][]; masked: unknown[][] }

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/anonymise`;
type Body = { name: string; rules: Rules; target: Target | null; scratch_path: string | null };

export const anonApi = {
  list: (sid: string, dbid: string) => request<{ profiles: Profile[] }>("GET", base(sid, dbid)),
  columns: (sid: string, dbid: string) => request<Schema>("GET", base(sid, dbid) + "/columns"),
  create: (sid: string, dbid: string, b: Body) => request<Profile>("POST", base(sid, dbid) + "/profiles", b),
  update: (sid: string, dbid: string, id: string, b: Body) => request<Profile>("PUT", base(sid, dbid) + `/profiles/${id}`, b),
  remove: (sid: string, dbid: string, id: string) => request<void>("DELETE", base(sid, dbid) + `/profiles/${id}`),
  check: (sid: string, dbid: string, id: string) =>
    request<{ columns: { table: string; column: string; strategy: Strategy; linked: boolean }[]; tables: number }>("POST", base(sid, dbid) + `/profiles/${id}/check`),
  preview: (sid: string, dbid: string, table: string, rules: Rules, profile_id?: string) =>
    request<Preview>("POST", base(sid, dbid) + "/preview", { table, rules, profile_id: profile_id ?? null, limit: 5 }),
  run: (sid: string, dbid: string, id: string) => request<Job>("POST", base(sid, dbid) + `/profiles/${id}/run`),
};

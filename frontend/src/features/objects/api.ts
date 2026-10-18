import { request } from "../../api";

const enc = encodeURIComponent;
const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}`;

export interface DdlScript {
  sql: string;
  statements: string[];
  warnings: string[];
}

export interface Lookups {
  engine_major: number;
  engine_version: string;
  default_charset: string;
  tables: string[];
  views: string[];
  domains: string[];
  charsets: string[];
  collations: Array<{ name: string; charset: string }>;
  system_privileges: string[];
}

export interface Grant {
  grantee: string;
  grantee_type: string;
  grantor?: string | null;
  privilege: string;
  object: string;
  object_type: string;
  column?: string | null;
  grant_option?: boolean;
}

export interface Dependent {
  name: string;
  type: string;
  depth?: number;
  via?: string;
  detail?: string | null;
  field?: string | null;
}

export interface DropPreview {
  sql: string;
  dependents: Dependent[];
  foreign_keys: Array<{ name: string; relation: string }>;
  columns: Array<{ relation: string; name: string }>;
  grants: Grant[];
  constraints?: string[];
  indices?: string[];
}

export const oapi = {
  lookups: (sid: string, dbid: string) => request<Lookups>("GET", base(sid, dbid) + "/designer/lookups"),
  spec: <T = any>(sid: string, dbid: string, otype: string, name: string) =>
    request<{ spec: T; engine_major: number }>("GET", base(sid, dbid) + `/designer/${otype}/${enc(name)}`),
  relation: (sid: string, dbid: string, table: string) =>
    request<{ columns: string[]; unique_keys: Array<{ name: string; type: string; columns: string[] }> }>(
      "GET", base(sid, dbid) + `/designer/relations/${enc(table)}`),
  comments: (sid: string, dbid: string, otype: string, name: string) =>
    request<any>("GET", base(sid, dbid) + `/designer/comments/${otype}/${enc(name)}`),
  ddl: (sid: string, dbid: string, kind: string, spec: unknown, original?: unknown) =>
    request<DdlScript>("POST", base(sid, dbid) + "/ddl", { kind, spec, original: original ?? null }),
  dropPreview: (sid: string, dbid: string, otype: string, name: string, column?: string) =>
    request<DropPreview>("GET", base(sid, dbid) + `/objects/${otype}/${enc(name)}/drop-preview` +
      (column ? `?column=${enc(column)}` : "")),
  privileges: (sid: string, dbid: string, q: { object?: string; grantee?: string }) => {
    const p = new URLSearchParams(Object.entries(q).filter(([, v]) => v) as Array<[string, string]>);
    return request<{ grants: Grant[]; columns?: string[] }>("GET", base(sid, dbid) + `/privileges?${p}`);
  },
  grantees: (sid: string, dbid: string) =>
    request<Record<string, string[]>>("GET", base(sid, dbid) + "/privileges/grantees"),
  grantable: (sid: string, dbid: string) =>
    request<Record<string, string[]>>("GET", base(sid, dbid) + "/privileges/objects"),
};

// lookups are cached per database for a short while (dialogs open often)
const cache = new Map<string, { at: number; p: Promise<Lookups> }>();
export function getLookups(sid: string, dbid: string, fresh = false): Promise<Lookups> {
  const k = `${sid}:${dbid}`;
  const hit = cache.get(k);
  if (!fresh && hit && Date.now() - hit.at < 15000) return hit.p;
  const p = oapi.lookups(sid, dbid);
  cache.set(k, { at: Date.now(), p });
  p.catch(() => cache.delete(k));
  return p;
}

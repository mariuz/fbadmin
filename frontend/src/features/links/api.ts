import { request } from "../../api";
import type { ColumnInfo } from "../../types";

export type LinkKind = "object" | "query" | "notebook" | "snapshot";
export interface Snapshot { sql: string; columns: ColumnInfo[]; rows: unknown[][]; row_count: number | null; truncated: boolean; taken_at: number }
export interface LinkInfo {
  id: string; kind: LinkKind; title: string; server_id: string | null; database_id: string | null; otype: string | null;
  name: string | null; schema_name: string | null; item_id: string | null; created_by: string | null; created_at: number;
  expires_at: number | null; opened: number; last_opened: number | null; path: string; rows?: number;
}
export interface Resolved {
  id: string; kind: LinkKind; title: string; created_by: string | null; created_at: number; expires_at: number | null;
  server_id?: string | null; database_id?: string | null; otype?: string; name?: string; schema?: string | null;
  server?: string; database?: string; item_id?: string; sql?: string; snapshot?: Snapshot | null;
}
export interface LinkIn {
  kind: LinkKind; title?: string; server_id?: string | null; database_id?: string | null; otype?: string; name?: string;
  schema?: string | null; item_id?: string; expires_days?: number | null;
  snapshot?: { sql: string; columns: ColumnInfo[]; rows: unknown[][]; row_count: number | null; truncated: boolean };
}

export const linkApi = {
  create: (b: LinkIn) => request<LinkInfo>("POST", "/api/links", b),
  list: (all = false) => request<{ links: LinkInfo[]; server_mode: boolean }>("GET", "/api/links" + (all ? "?all=true" : "")),
  /** ``denied``: the reason and who shared it, when the viewer's permissions do not allow it (403). */
  open: async (id: string): Promise<{ ok: Resolved } | { denied: string; by: string | null } | { gone: string }> => {
    const res = await fetch(`/api/links/${encodeURIComponent(id)}`, { headers: { Accept: "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (res.ok) return { ok: data as Resolved };
    if (res.status === 403 && data.code === "link_denied") return { denied: String(data.detail), by: data.created_by ?? null };
    if (res.status === 401) return { gone: String(data.detail || "Sign in first") };
    return { gone: String(data.detail || res.statusText) };
  },
  revoke: (id: string) => request<void>("DELETE", `/api/links/${encodeURIComponent(id)}`),
};

export const fullUrl = (path: string) => `${location.origin}${location.pathname.replace(/\/$/, "")}${path.replace(/^\//, "/")}`;

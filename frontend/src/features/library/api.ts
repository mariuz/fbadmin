import { request, type Body } from "../../api";

export type Visibility = "private" | "shared";

export interface LibraryItem {
  id: string; name: string; description: string; sql: string; tags: string[]; folder: string;
  server_id: string | null; database_id: string | null; visibility: Visibility;
  owner_id: number | null; owner: string | null; created_at: number; updated_at: number; updated_by: string | null;
  rev: number; can_edit: boolean; mine: boolean; revision_count: number;
  revisions?: LibraryRevision[];
}

export interface LibraryRevision {
  rev: number; at: number; author: string | null; name: string; description: string; sql: string; tags: string[];
  folder: string; server_id: string | null; database_id: string | null; visibility: Visibility;
}

export interface LibraryInfo {
  server_mode: boolean; user: string | null; is_admin: boolean;
  folders: Array<{ path: string; count: number }>; tags: Array<{ tag: string; count: number }>;
}

export interface LibraryQuery { q?: string; tag?: string; folder?: string; scope?: "all" | "mine" | "shared" }

export type LibraryItemIn = Body<"/api/library/items", "post">;
export type LibraryItemUpdate = Body<"/api/library/items/{item_id}", "put">;

export const lapi = {
  info: () => request<LibraryInfo>("GET", "/api/library"),
  items: (q: LibraryQuery = {}) => {
    const p = new URLSearchParams(Object.entries(q).filter(([, v]) => v).map(([k, v]) => [k, String(v)]));
    return request<LibraryItem[]>("GET", `/api/library/items?${p}`);
  },
  item: (id: string) => request<LibraryItem>("GET", `/api/library/items/${id}`),
  create: (body: LibraryItemIn) => request<LibraryItem>("POST", "/api/library/items", body),
  update: (id: string, body: LibraryItemUpdate) => request<LibraryItem>("PUT", `/api/library/items/${id}`, body),
  remove: (id: string) => request<void>("DELETE", `/api/library/items/${id}`),
  copy: (id: string, name?: string) => request<LibraryItem>("POST", `/api/library/items/${id}/copy`, { name: name ?? null }),
  restore: (id: string, rev: number) => request<LibraryItem>("POST", `/api/library/items/${id}/revisions/${rev}/restore`),
  importSnippets: (items: Array<{ name: string; sql: string }>) =>
    request<{ imported: number }>("POST", "/api/library/import", { items }),
};

// --- snippets kept in the browser by the Query Tool (fbadmin.snippets) --------------------
const SNIPPETS_KEY = "fbadmin.snippets";
const MIGRATED_KEY = "fbadmin.library.snippetsImported";
let migrating: Promise<number> | null = null;

/** Copy the Query Tool's browser-stored snippets into the library once per browser (the server skips
 *  ones already there). The snippets stay in the browser, so the Query Tool's Snippets pane keeps working. */
export function migrateBrowserSnippets(user: string | null): Promise<number> {
  if (migrating) return migrating;
  const marker = user ? `${MIGRATED_KEY}:${user}` : MIGRATED_KEY;
  migrating = (async () => {
    let raw: string | null = null;
    try {
      if (localStorage.getItem(marker)) return 0;
      raw = localStorage.getItem(SNIPPETS_KEY);
    } catch { return 0; }
    let n = 0;
    if (raw) {
      try {
        const list = JSON.parse(raw) as Array<{ name?: unknown; sql?: unknown }>;
        const items = (Array.isArray(list) ? list : [])
          .filter((s) => typeof s?.name === "string" && typeof s?.sql === "string")
          .map((s) => ({ name: s.name as string, sql: s.sql as string }));
        if (items.length) n = (await lapi.importSnippets(items)).imported;
      } catch (e) {
        migrating = null;
        throw e;
      }
    }
    try { localStorage.setItem(marker, String(Date.now())); } catch { /* storage unavailable */ }
    return n;
  })();
  return migrating;
}

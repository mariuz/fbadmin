import { request, type Body } from "../../api";
import type { DatabaseEntry } from "../../types";

export interface EmbeddedStatus {
  available: boolean;
  /** Why local databases are unavailable (empty when available). */
  reason: string;
  directory: string | null;
  version: string | null;
  /** Suggested folder for new local databases. */
  data_directory: string;
  /** The "Local databases" registration, once it exists. */
  server_id: string | null;
}

export const eapi = {
  status: () => request<EmbeddedStatus>("GET", "/api/embedded/status"),
  add: (body: Body<"/api/embedded/databases", "post">) =>
    request<{ server_id: string; database: DatabaseEntry; created: boolean }>("POST", "/api/embedded/databases", body),
};

let cached: Promise<EmbeddedStatus> | null = null;
/** Status, fetched once per page load (it only changes with the process environment). */
export function embeddedStatus(): Promise<EmbeddedStatus> {
  if (!cached) {
    cached = eapi.status();
    cached.catch(() => { cached = null; });
  }
  return cached;
}

/** Native file dialog of the desktop app (pywebview js_api, see backend/fbadmin/desktop.py). */
type Picker = (save: boolean, suggested: string) => Promise<string | null>;
export function desktopFilePicker(): Picker | null {
  const api = (window as unknown as { pywebview?: { api?: { choose_database_file?: Picker } } }).pywebview?.api;
  return api?.choose_database_file ? api.choose_database_file.bind(api) : null;
}

import { request } from "../../api";

export interface EventNameState {
  name: string; count: number; last_at: number | null; registered_at: number; listening: boolean;
  /** arrivals per bucket over the last ``rate_window`` seconds (oldest first) */
  rate: number[];
}

export interface EventSession {
  id: string; server_id: string; db_id: string;
  status: "starting" | "listening" | "error" | "expired" | "closed";
  error: string | null; error_code: string | null; created_at: number; now: number;
  requests: number; notifications: number; last_seq: number; rate_window: number;
  names: EventNameState[];
}

export interface EventArrival { seq: number; time: number; name: string; count: number; total: number }

export interface EventChanges { session: EventSession; log: EventArrival[]; added?: string[] }

export interface EventSource { type: "trigger" | "procedure" | "function" | "package"; name: string; parent: string | null }

export interface EventSuggestions {
  names: Array<{ name: string; sources: EventSource[] }>;
  dynamic: Array<EventSource & { expression: string }>;
}

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/event-monitor`;
const enc = encodeURIComponent;

export const eapi = {
  suggestions: (sid: string, dbid: string) => request<EventSuggestions>("GET", `${base(sid, dbid)}/suggestions`),
  open: (sid: string, dbid: string, names: string[]) => request<EventChanges>("POST", `${base(sid, dbid)}/sessions`, { names }),
  poll: (sid: string, dbid: string, id: string, after: number) =>
    request<EventChanges>("GET", `${base(sid, dbid)}/sessions/${enc(id)}?after=${after}`),
  register: (sid: string, dbid: string, id: string, names: string[]) =>
    request<EventChanges>("POST", `${base(sid, dbid)}/sessions/${enc(id)}/register`, { names }),
  unregister: (sid: string, dbid: string, id: string, names: string[]) =>
    request<EventChanges>("POST", `${base(sid, dbid)}/sessions/${enc(id)}/unregister`, { names }),
  reset: (sid: string, dbid: string, id: string) => request<EventChanges>("POST", `${base(sid, dbid)}/sessions/${enc(id)}/reset`),
  reconnect: (sid: string, dbid: string, id: string) =>
    request<EventChanges>("POST", `${base(sid, dbid)}/sessions/${enc(id)}/reconnect`),
  close: (sid: string, dbid: string, id: string) => request<void>("DELETE", `${base(sid, dbid)}/sessions/${enc(id)}`),
  post: (sid: string, dbid: string, name: string, count: number) =>
    request<{ name: string; count: number }>("POST", `${base(sid, dbid)}/post`, { name, count }),
};

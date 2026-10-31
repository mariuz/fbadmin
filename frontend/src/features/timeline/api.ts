import { request } from "../../api";

export type Category = "schema" | "data" | "backup" | "maintenance" | "upgrade" | "alert" | "audit";
export interface TimelineEvent {
  id: string; ts: number; category: Category; action: string; title: string; detail: string | null;
  status: "ok" | "failed" | "warning" | "info" | "running" | "cancelled"; user: string | null; source: string;
  ref: { type?: string; [k: string]: any }; finished_at: number | null; error: string | null;
}
export interface Timeline {
  events: TimelineEvent[]; total: number; has_more: boolean; counts: Record<Category, number>;
  problems: { source: string; error: string }[]; audit: boolean; engine_version?: string; ods?: string;
}

export const timelineApi = {
  get: (sid: string, dbid: string, p: { since?: number; until?: number; category?: string; q?: string; limit?: number; audit_queries?: boolean }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== "" && v !== false) qs.set(k, String(v));
    return request<Timeline>("GET", `/api/servers/${sid}/databases/${dbid}/timeline${qs.size ? "?" + qs : ""}`);
  },
};

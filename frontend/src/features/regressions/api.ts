import { request } from "../../api";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/monitoring`;

export interface RegSettings {
  regression_factor: number; regression_recent_hours: number; regression_baseline_days: number; regression_min_runs: number;
  regression_min_seconds: number; regression_query_ms: number; regression_keep_days: number; slow_seconds: number;
}
export interface RegStatement {
  sql_hash: string; normalized: string; example: string; first_seen: number; last_seen: number; accepted_at: number | null;
  runs: number; baseline_runs: number; recent_runs: number; baseline_median: number | null; recent_median: number | null;
  ratio: number | null; max_seconds: number; baseline_plan: string | null; recent_plan: string | null;
  flags: Array<"slower" | "plan_changed">; regression: boolean;
}
export interface RegDetail {
  sql_hash: string; normalized: string; example: string; accepted_at: number | null; analysis: RegStatement | null;
  runs: Array<{ ts: number; seconds: number; plan_hash: string | null; source: string }>;
  plans: Array<{ plan_hash: string; plan: string; first_seen: number; last_seen: number; runs: number; median: number | null }>;
}

export const regApi = {
  list: (sid: string, dbid: string, only: string) =>
    request<{ tracking: boolean; settings: RegSettings; regressions: number; statements: RegStatement[] }>("GET", `${base(sid, dbid)}/regressions?only=${only}`),
  detail: (sid: string, dbid: string, h: string) => request<RegDetail>("GET", `${base(sid, dbid)}/regressions/${h}`),
  accept: (sid: string, dbid: string, h: string) => request<{ ok: boolean }>("POST", `${base(sid, dbid)}/regressions/${h}/accept`),
  track: (sid: string, dbid: string) => request<unknown>("PUT", `${base(sid, dbid)}/history/tracking`, { enabled: true }),
  saveSettings: (s: Partial<RegSettings>) => request<unknown>("PUT", "/api/monitoring/settings", s),
};

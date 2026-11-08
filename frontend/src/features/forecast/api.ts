import { request } from "../../api";

export type Sev = "ok" | "warning" | "critical";
export interface SeqRow {
  name: string; value: number; per_day: number | null; limit: number; pct: number; days_left: number | null;
  table: string | null; column: string | null; type: string; how: "identity" | "trigger" | null; severity: Sev;
}
export interface Forecast {
  observations: number; window_observations: number; first: number | null; last: number | null; window_days: number;
  enough: boolean; tracked: boolean;
  size: { bytes: number; per_day: number | null; budget: number | null; budget_pct: number | null; days_left: number | null;
    in_30: number | null; in_90: number | null; in_365: number | null; series: [number, number][]; severity: Sev };
  transactions: { next: number; limit: number; per_day: number | null; days_left: number | null; pct: number; severity: Sev };
  sequences: SeqRow[];
  formats: { table: string; formats: number; limit: number; severity: Sev }[];
  findings: { key: string; severity: Sev; title: string }[];
}

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/monitoring/forecast`;
export const fcApi = {
  get: (sid: string, dbid: string) => request<Forecast>("GET", base(sid, dbid)),
  observe: (sid: string, dbid: string) => request<Forecast>("POST", base(sid, dbid) + "/observe"),
  budget: (sid: string, dbid: string, budget_bytes: number | null) => request<{ budget_bytes: number | null }>("PUT", base(sid, dbid) + "/budget", { budget_bytes }),
};

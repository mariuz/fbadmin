import { request } from "../../api";
import type { Job } from "../../types";

const ubase = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/upgrade`;

export type Severity = "blocker" | "warning" | "info";

export interface UpgradeObject {
  type: string;
  name: string;
  parent: string | null;
  link: { otype: string; name: string } | null;
  detail: string | null;
  fix: string | null;
}

export interface Finding {
  id: string;
  category: string;
  severity: Severity;
  title: string;
  explanation: string;
  fix: string;
  objects: UpgradeObject[];
  refs: string[];
}

export interface PlanStep {
  title: string;
  text: string[];
  commands: string[];
  table: { columns: string[]; rows: string[][] } | null;
}

export interface UpgradeReport {
  database: { name: string; path: string; host: string; port: number; server: string };
  source: {
    engine_version: string; version: string; ods: string; dialect: number; page_size: number; size_bytes: number;
    charset: string; counts: Record<string, number>;
  };
  target: { version: string; ods: string };
  target_server?: { id: string; name: string; host: string; port: number };
  trial?: {
    scratch: string; kept: boolean; registered_as?: string; target_engine?: string; seconds?: number;
    restored?: { ods: string; page_size: number; engine_version: string };
  };
  findings: Finding[];
  counts: Record<Severity, number>;
  plan: PlanStep[];
  generated_at: string;
}

export interface TrialResult {
  status: "running" | "finished" | "failed";
  error: string | null;
  report?: UpgradeReport;
}

export const upgradeApi = {
  versions: () => request<{ versions: string[]; ods: Record<string, string> }>("GET", "/api/upgrade/versions"),
  check: (sid: string, dbid: string, target: string) =>
    request<UpgradeReport>("POST", ubase(sid, dbid) + "/check", { target }),
  trial: (sid: string, dbid: string, body: {
    target_server_id: string; target?: string; scratch_path?: string | null; metadata_only: boolean; keep: boolean;
  }) => request<Job>("POST", ubase(sid, dbid) + "/trial", body),
  trialResult: (jobId: string) => request<TrialResult>("GET", `/api/upgrade/jobs/${jobId}/result`),
  render: (report: UpgradeReport, format: "md" | "html") =>
    request<{ filename: string; media_type: string; content: string }>("POST", "/api/upgrade/render", { report, format }),
};

import { ApiError, request, type Body } from "../../api";
import type { Job } from "../../types";
import { t } from "../../i18n";

const enc = encodeURIComponent;
const sbase = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/schema`;

export interface DiffItem {
  type: string;
  name: string;
  status: "create" | "alter" | "drop";
  source_ddl: string | null;
  target_ddl: string | null;
}
export interface DiffResult {
  items: DiffItem[];
  summary: Record<string, number>;
  warnings: string[];
  script: string;
  source_name: string;
  target_name: string;
}
export interface DiffSide { server_id: string; db_id?: string; sql?: string }

export interface ErdColumn {
  name: string; type: string; data_type: string; pk: boolean; fk: boolean; unique: boolean; not_null: boolean;
  computed: boolean;
}
export interface ErdTable { name: string; comment: string | null; relation_type: number; columns: ErdColumn[] }
export interface ErdRelation {
  name: string; from_table: string; from_columns: string[]; to_table: string; to_columns: string[];
  on_delete?: string; on_update?: string;
}

export interface UploadInfo { upload_id: string; filename: string; size: number; format: string }
export interface PreviewColumn { name: string; suggested_name: string; type: string }
export interface Preview extends UploadInfo {
  columns: PreviewColumn[];
  rows: unknown[][];
  sampled_rows: number;
  sheets: string[];
  delimiter: string | null;
}

export interface JobResult {
  status: "running" | "finished" | "failed";
  error: string | null;
  inserted?: number;
  failed?: number;
  errors?: Array<{ row: number; message: string }>;
  tables?: Record<string, number>;
  rows?: number;
  created?: boolean;
}

export interface SearchItem {
  type: string; name: string; parent: string | null; parent_type?: string; package?: string;
  match: "name" | "comment" | "source"; snippet: string | null; line?: number | null;
}

export interface ReportSection {
  id: string; title: string; description: string;
  items: Array<{ type: string; name: string; parent: string | null; detail: string | null }>;
}

export const schemaApi = {
  extract: (sid: string, dbid: string, opts: Record<string, boolean>) => {
    const q = new URLSearchParams(Object.entries(opts).map(([k, v]) => [k, String(v)]));
    return request<{ sql: string }>("GET", `${sbase(sid, dbid)}/extract?${q}`);
  },
  extractUrl: (sid: string, dbid: string, opts: Record<string, boolean>) => {
    const q = new URLSearchParams(Object.entries({ ...opts, download: true }).map(([k, v]) => [k, String(v)]));
    return `${sbase(sid, dbid)}/extract?${q}`;
  },
  diff: (source: DiffSide, target: DiffSide, set_term: boolean) =>
    request<DiffResult>("POST", "/api/schema/diff", { source, target, set_term }),
  erd: (sid: string, dbid: string) =>
    request<{ tables: ErdTable[]; relations: ErdRelation[] }>("GET", `${sbase(sid, dbid)}/erd`),
  preview: (uploadId: string, format: string | null, options: Record<string, unknown>) =>
    request<Preview>("POST", `/api/schema/uploads/${uploadId}/preview`, { format, options }),
  importData: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/schema/import", "post">) =>
    request<Job>("POST", `${sbase(sid, dbid)}/import`, body),
  exportUrl: (sid: string, dbid: string, table: string, format: string, delimiter: string, header: boolean) =>
    `${sbase(sid, dbid)}/export?table=${enc(table)}&format=${format}&delimiter=${enc(delimiter)}&header=${header}`,
  copy: (body: Body<"/api/schema/copy", "post">) => request<Job>("POST", "/api/schema/copy", body),
  search: (sid: string, dbid: string, q: string, scopes: string[], types: string[]) =>
    request<{ items: SearchItem[]; truncated: boolean }>(
      "GET", `${sbase(sid, dbid)}/search?q=${enc(q)}&scopes=${scopes.join(",")}&types=${types.join(",")}`),
  generate: (sid: string, dbid: string, body: Body<"/api/servers/{server_id}/databases/{db_id}/schema/generate", "post">) =>
    request<Job>("POST", `${sbase(sid, dbid)}/generate`, body),
  report: (sid: string, dbid: string) => request<{ sections: ReportSection[] }>("GET", `${sbase(sid, dbid)}/report`),
  jobResult: (id: string) => request<JobResult>("GET", `/api/schema/jobs/${id}/result`),
};

/** Upload a file as the raw request body (streams on both ends), with progress. */
export function uploadFile(file: File, onProgress?: (fraction: number) => void): Promise<UploadInfo> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/schema/uploads?filename=${enc(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let data: any = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, data.detail || xhr.statusText));
    };
    xhr.onerror = () => reject(new Error(t("Upload failed")));
    xhr.send(file);
  });
}

/** POST and save the response as a file (used for query exports). */
export async function postDownload(url: string, body: unknown, fallbackName: string) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(res.status, data.detail || res.statusText);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="([^"]+)"/);
  saveBlob(await res.blob(), m ? m[1] : fallbackName);
}

export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function saveUrl(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

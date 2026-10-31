import { request } from "../../api";
import type { ColumnInfo } from "../../types";
import type { ChartConfig } from "../charts/shape";

export interface ResultOut {
  kind: "select" | "command" | "ignored" | "error"; message: string; columns: ColumnInfo[]; rows: unknown[][];
  row_count: number | null; truncated: boolean; elapsed_ms: number | null;
}
export interface Output { ran_at: number; ran_by: string | null; elapsed_ms: number | null; error: string | null; results: ResultOut[] }
export interface Cell { id: string; type: "sql" | "markdown"; source: string; chart: ChartConfig | null; output: Output | null }
export interface NotebookMeta {
  id: string; name: string; description: string; server_id: string | null; database_id: string | null;
  visibility: "private" | "shared"; owner: string | null; created_at: number; updated_at: number; updated_by: string | null;
  rev: number; can_edit: boolean; mine: boolean; cell_count: number; revision_count: number;
}
export interface Notebook extends NotebookMeta { cells: Cell[]; results_hidden: boolean }
export interface RevisionInfo { rev: number; at: number; author: string | null; name: string; cells: number }

const B = "/api/notebooks";
const q = (p: Record<string, string | undefined>) => {
  const s = new URLSearchParams(Object.entries(p).filter(([, v]) => v) as [string, string][]).toString();
  return s ? "?" + s : "";
};

export const nbApi = {
  list: (p: { q?: string; scope?: string; server_id?: string; database_id?: string } = {}) =>
    request<{ server_mode: boolean; user: string | null; notebooks: NotebookMeta[] }>("GET", B + q(p)),
  get: (id: string) => request<Notebook>("GET", `${B}/${id}`),
  create: (body: { name: string; server_id?: string | null; database_id?: string | null; cells?: Cell[] }) =>
    request<Notebook>("POST", B, body),
  save: (id: string, body: Partial<Pick<Notebook, "name" | "description" | "server_id" | "database_id" | "visibility" | "cells">> & { base_rev?: number }) =>
    request<Notebook>("PUT", `${B}/${id}`, body),
  remove: (id: string) => request<void>("DELETE", `${B}/${id}`),
  copy: (id: string, name?: string) => request<Notebook>("POST", `${B}/${id}/copy`, { name: name || null }),
  revisions: (id: string) => request<{ revisions: RevisionInfo[] }>("GET", `${B}/${id}/revisions`),
  restore: (id: string, rev: number) => request<Notebook>("POST", `${B}/${id}/revisions/${rev}/restore`),
  importDoc: (notebook: unknown, server_id?: string | null, database_id?: string | null) =>
    request<Notebook>("POST", `${B}/import`, { notebook, server_id, database_id }),
  exportUrl: (id: string, format: "json" | "md", results = true) => `${B}/${id}/export${q({ format, results: results ? undefined : "false" })}`,
};

export const newId = () => Math.random().toString(16).slice(2, 14).padEnd(12, "0");

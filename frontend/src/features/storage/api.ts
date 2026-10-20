import { request, type Body } from "../../api";

export type TargetKind = "s3" | "azure" | "gcs";

export interface StorageTarget {
  id: string; name: string; kind: TargetKind; created_at: number;
  settings: Record<string, string | boolean>;
  [k: `has_${string}`]: boolean | undefined;
}

export interface StoredObject { key: string; size: number; modified: number }

export const sapi = {
  targets: () => request<StorageTarget[]>("GET", "/api/storage/targets"),
  create: (body: Body<"/api/storage/targets", "post">) => request<StorageTarget>("POST", "/api/storage/targets", body),
  update: (id: string, body: Body<"/api/storage/targets/{target_id}", "put">) => request<StorageTarget>("PUT", `/api/storage/targets/${id}`, body),
  remove: (id: string) => request<void>("DELETE", `/api/storage/targets/${id}`),
  test: (id: string) => request<{ ok: boolean; details?: unknown; error?: string }>("POST", `/api/storage/targets/${id}/test`),
  objects: (id: string, prefix = "") => request<StoredObject[]>("GET", `/api/storage/targets/${id}/objects?prefix=${encodeURIComponent(prefix)}`),
};

import { request } from "../../api";
import type { QbColumn, QbForeignKey } from "./model";

const base = (sid: string, dbid: string) => `/api/servers/${sid}/databases/${dbid}/query-builder`;
const q = (schema?: string | null) => (schema ? `?schema=${encodeURIComponent(schema)}` : "");

export interface RelationList {
  engine_major: number;
  schemas_supported: boolean;
  /** schema the list belongs to (Firebird 6), else null */
  schema: string | null;
  schemas: string[];
  relations: Array<{ name: string; kind: "table" | "view"; description: string | null }>;
  reserved: string[];
}
export interface RelationInfo {
  name: string;
  schema: string | null;
  columns: QbColumn[];
  foreign_keys: QbForeignKey[];
}

export const qbApi = {
  relations: (sid: string, dbid: string, schema?: string | null) => request<RelationList>("GET", `${base(sid, dbid)}/relations${q(schema)}`),
  relation: (sid: string, dbid: string, name: string, schema?: string | null) =>
    request<RelationInfo>("GET", `${base(sid, dbid)}/relations/${encodeURIComponent(name)}${q(schema)}`),
};

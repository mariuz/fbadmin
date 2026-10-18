export interface DatabaseEntry {
  id: string;
  name: string;
  path: string;
  connected?: boolean;
}

export interface Server {
  id: string;
  name: string;
  host: string;
  port: number;
  user: string;
  role: string | null;
  charset: string;
  wire_crypt: boolean;
  save_password: boolean;
  group: string;
  has_password: boolean;
  password_known: boolean;
  connected: boolean;
  databases: DatabaseEntry[];
}

export type NodeKind = "server" | "database" | "folder" | "object" | "column";

/** A node selected in the object explorer. */
export interface Selection {
  kind: NodeKind;
  serverId: string;
  dbId?: string;
  otype?: string; // tables, views, ...
  name?: string; // object name
  column?: string;
}

export interface ColumnInfo {
  name: string;
  type: string;
  nullable?: boolean;
}

export interface StatementResult {
  index: number;
  line: number;
  sql: string;
  kind: "select" | "command" | "ignored" | "error";
  columns?: ColumnInfo[];
  rows?: unknown[][];
  row_count?: number;
  truncated?: boolean;
  message?: string;
  plan?: string | null;
  elapsed_ms: number;
}

export interface ScriptResult {
  results: StatementResult[];
  error: { message: string; index: number; line: number; sql: string } | null;
  elapsed_ms: number;
  transaction_open: boolean;
}

export interface Job {
  id: string;
  kind: string;
  description: string;
  status: "running" | "finished" | "failed";
  started_at: number;
  finished_at: number | null;
  error: string | null;
  log?: string[];
  log_lines?: number;
}

export interface ObjectDetail {
  type: string;
  name: string;
  properties: Record<string, unknown>;
  ddl: string;
  columns?: Array<Record<string, any>>;
  constraints?: Array<Record<string, any>>;
  indices?: Array<Record<string, any>>;
  triggers?: Array<Record<string, any>>;
  parameters?: Array<Record<string, any>>;
  returns?: string;
  dependencies?: {
    depends_on: Array<{ name: string; type: string; field: string | null }>;
    dependents: Array<{ name: string; type: string; field: string | null }>;
  };
}

export const OBJECT_TYPES: Array<{ type: string; label: string; singular: string }> = [
  { type: "tables", label: "Tables", singular: "Table" },
  { type: "views", label: "Views", singular: "View" },
  { type: "procedures", label: "Procedures", singular: "Procedure" },
  { type: "functions", label: "Functions", singular: "Function" },
  { type: "packages", label: "Packages", singular: "Package" },
  { type: "triggers", label: "Triggers", singular: "Trigger" },
  { type: "sequences", label: "Sequences", singular: "Sequence" },
  { type: "domains", label: "Domains", singular: "Domain" },
  { type: "exceptions", label: "Exceptions", singular: "Exception" },
  { type: "indices", label: "Indices", singular: "Index" },
  { type: "roles", label: "Roles", singular: "Role" },
  { type: "users", label: "Users", singular: "User" },
  { type: "system_tables", label: "System Tables", singular: "System Table" },
];

export const typeInfo = (t?: string) => OBJECT_TYPES.find((o) => o.type === t);

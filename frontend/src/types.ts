import { N_ } from "./i18n";

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
  wire_compress?: boolean;
  auth_plugin?: string | null;
  read_only?: boolean;
  shared?: boolean;
  /** Local database files opened with the embedded engine (features/embedded). */
  embedded?: boolean;
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
  /** Firebird 6 SQL schema the node belongs to. */
  schema?: string;
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
  col?: number;
  kind: "select" | "command" | "ignored" | "error";
  columns?: ColumnInfo[];
  rows?: unknown[][];
  row_count?: number;
  truncated?: boolean;
  message?: string;
  plan?: string | null;
  stats?: Record<string, number> | null;
  elapsed_ms: number;
}

export interface ScriptResult {
  results: StatementResult[];
  error: {
    message: string;
    index: number;
    line: number;
    sql: string;
    position?: { line: number; column: number } | null;
  } | null;
  elapsed_ms: number;
  transaction_open: boolean;
  cursor_open?: boolean;
}

export interface SessionOptions {
  isolation: string;
  read_only: boolean;
  wait: boolean;
  lock_timeout: number | null;
  statement_timeout: number;
  engine_major: number;
  attachment_id: number | null;
  /** Firebird 6: the session's search path, e.g. "APP", "SYSTEM" */
  search_path?: string | null;
}

export interface Completion {
  relations: Record<string, string[]>;
  procedures: string[];
  functions: string[];
  packages: Record<string, string[]>;
}

export interface Job {
  id: string;
  kind: string;
  description: string;
  status: "running" | "finished" | "failed" | "cancelled";
  started_at: number;
  finished_at: number | null;
  error: string | null;
  log?: string[];
  log_lines?: number;
  /** the job can be stopped with api.cancelJob */
  cancellable?: boolean;
  cancel_requested?: boolean;
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
  { type: "tables", label: N_("Tables"), singular: N_("Table") },
  { type: "views", label: N_("Views"), singular: N_("View") },
  { type: "procedures", label: N_("Procedures"), singular: N_("Procedure") },
  { type: "functions", label: N_("Functions"), singular: N_("Function") },
  { type: "packages", label: N_("Packages"), singular: N_("Package") },
  { type: "triggers", label: N_("Triggers"), singular: N_("Trigger") },
  { type: "sequences", label: N_("Sequences"), singular: N_("Sequence") },
  { type: "domains", label: N_("Domains"), singular: N_("Domain") },
  { type: "exceptions", label: N_("Exceptions"), singular: N_("Exception") },
  { type: "indices", label: N_("Indices"), singular: N_("Index") },
  { type: "roles", label: N_("Roles"), singular: N_("Role") },
  { type: "users", label: N_("Users"), singular: N_("User") },
  { type: "system_tables", label: N_("System Tables"), singular: N_("System Table") },
  { type: "collations", label: N_("Collations"), singular: N_("Collation") },
  { type: "charsets", label: N_("Character Sets"), singular: N_("Character Set") },
  { type: "shadows", label: N_("Shadows"), singular: N_("Shadow") },
  { type: "blob_filters", label: N_("BLOB Filters"), singular: N_("BLOB Filter") },
];

export const typeInfo = (t?: string) => OBJECT_TYPES.find((o) => o.type === t);

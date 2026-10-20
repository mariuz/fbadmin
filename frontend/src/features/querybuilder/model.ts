/**
 * Visual query builder: the builder state and the SELECT it generates.
 *
 * Pure module (no React, no i18n, no imports) so it can be unit tested with `node --test`
 * (frontend/tests/querybuilder.test.ts). Messages are returned as stable codes; the UI translates.
 */

export type JoinType = "INNER" | "LEFT" | "RIGHT" | "FULL";
export type Agg = "" | "COUNT" | "COUNT DISTINCT" | "SUM" | "AVG" | "MIN" | "MAX" | "LIST";
export type SortDir = "" | "ASC" | "DESC";
export type LimitStyle = "first" | "rows" | "fetch";
export const JOIN_TYPES: JoinType[] = ["INNER", "LEFT", "RIGHT", "FULL"];
export const AGGS: Agg[] = ["", "COUNT", "COUNT DISTINCT", "SUM", "AVG", "MIN", "MAX", "LIST"];
export const OPERATORS = [
  "=", "<>", "<", "<=", ">", ">=", "LIKE", "NOT LIKE", "CONTAINING", "STARTING WITH", "IN", "NOT IN",
  "BETWEEN", "IS NULL", "IS NOT NULL",
] as const;
export type Operator = (typeof OPERATORS)[number];
export const NO_VALUE_OPS: Operator[] = ["IS NULL", "IS NOT NULL"];

export interface QbColumn { name: string; type: string; pk?: boolean; fk?: boolean; not_null?: boolean }
export interface QbForeignKey { name: string; columns: string[]; ref_table: string; ref_columns: string[] }

export interface QbTable {
  id: string;
  /** Firebird 6 schema of the relation (null on older servers). */
  schema: string | null;
  name: string;
  kind: "table" | "view";
  alias: string;
  columns: QbColumn[];
  foreign_keys: QbForeignKey[];
  x: number;
  y: number;
}
export interface QbJoinCond { left: string; op: string; right: string }
export interface QbJoin {
  id: string;
  type: JoinType;
  /** table ids */
  left: string;
  right: string;
  conditions: QbJoinCond[];
  /** Free SQL that replaces the generated ON condition when not empty. */
  custom: string;
  fk?: string;
}
export interface QbField {
  id: string;
  /** table id and column; or an expression when ``table`` is empty */
  table: string;
  column: string;
  expr: string;
  agg: Agg;
  alias: string;
  output: boolean;
  sort: SortDir;
  /** position in ORDER BY (lower first) */
  sortOrder: number;
}
export interface QbCriterion {
  id: string;
  table: string;
  column: string;
  expr: string;
  /** aggregated criteria go to HAVING */
  agg: Agg;
  op: Operator;
  value: string;
  value2: string;
  connector: "AND" | "OR";
}
export interface QbState {
  version: 1;
  tables: QbTable[];
  joins: QbJoin[];
  fields: QbField[];
  criteria: QbCriterion[];
  distinct: boolean;
  limitStyle: LimitStyle;
  limit: number | null;
  offset: number | null;
}

export function emptyState(): QbState {
  return { version: 1, tables: [], joins: [], fields: [], criteria: [], distinct: false, limitStyle: "fetch", limit: null, offset: null };
}

let seq = 0;
export function newId(prefix: string): string {
  seq += 1;
  return `${prefix}${Date.now().toString(36)}${seq}`;
}

// ------------------------------------------------------------------ identifiers
const PLAIN = /^[A-Z][A-Z0-9_$]*$/;
/** Words that force quoting. Replaced by the server's list (backend metadata/types.RESERVED) when known. */
let reserved = new Set<string>([
  "ADD", "ALL", "AND", "AS", "AVG", "BY", "CASE", "CHAR", "COLUMN", "COUNT", "CURRENT", "DATE", "DAY", "DEFAULT", "DELETE",
  "DISTINCT", "END", "FROM", "FULL", "GROUP", "HAVING", "IN", "INNER", "INSERT", "IS", "JOIN", "KEY", "LEFT", "LIKE", "MAX",
  "MIN", "MONTH", "NOT", "NULL", "ON", "OR", "ORDER", "POSITION", "RIGHT", "ROWS", "SELECT", "SET", "SUM", "TABLE", "TIME",
  "TIMESTAMP", "TO", "UPDATE", "USER", "VALUE", "VALUES", "WHERE", "YEAR",
]);
export function setReservedWords(words: string[]) {
  if (words.length) reserved = new Set(words.map((w) => w.toUpperCase()));
}
/** Same rule as the backend's ``quote_ident``: quote unless plain upper case and not reserved. */
export function quoteIdent(name: string): string {
  return PLAIN.test(name) && !reserved.has(name) ? name : `"${name.replace(/"/g, '""')}"`;
}
/** A name typed by the user (alias): plain words are upper-cased like Firebird does, others kept as typed. */
export function userIdent(name: string): string {
  const n = name.trim();
  return /^[A-Za-z][A-Za-z0-9_$]*$/.test(n) ? quoteIdent(n.toUpperCase()) : quoteIdent(n);
}
export function relationRef(t: Pick<QbTable, "schema" | "name">): string {
  return (t.schema ? quoteIdent(t.schema) + "." : "") + quoteIdent(t.name);
}

/** A short unique alias from the table name: CUSTOMER -> C, ORDER_LINES -> OL, then OL2, OL3… */
export function suggestAlias(name: string, taken: string[]): string {
  const parts = name.replace(/[^A-Za-z0-9_]/g, "").split("_").filter(Boolean);
  let base = parts.map((p) => p[0]).join("").toUpperCase() || "T";
  if (!/^[A-Z]/.test(base)) base = "T" + base;
  const used = new Set(taken.map((a) => a.toUpperCase()));
  const ok = (a: string) => !used.has(a) && !reserved.has(a);
  if (ok(base)) return base;
  for (let i = 2; ; i++) if (ok(base + i)) return base + i;
}

// ------------------------------------------------------------------ joins from foreign keys
/** Joins suggested by foreign keys between ``added`` and the tables already on the canvas. */
export function suggestJoins(added: QbTable, others: QbTable[]): QbJoin[] {
  const out: QbJoin[] = [];
  const sameSchema = (a: QbTable, b: QbTable) => (a.schema ?? "") === (b.schema ?? "");
  for (const o of others) {
    if (o.id === added.id) continue;
    for (const fk of added.foreign_keys) {
      if (fk.ref_table === o.name && sameSchema(added, o)) {
        out.push({
          id: newId("j"), type: "INNER", left: o.id, right: added.id, fk: fk.name, custom: "",
          conditions: fk.columns.map((c, i) => ({ left: fk.ref_columns[i] ?? c, op: "=", right: c })),
        });
      }
    }
    for (const fk of o.foreign_keys) {
      if (fk.ref_table === added.name && sameSchema(added, o)) {
        out.push({
          id: newId("j"), type: "INNER", left: o.id, right: added.id, fk: fk.name, custom: "",
          conditions: fk.columns.map((c, i) => ({ left: c, op: "=", right: fk.ref_columns[i] ?? c })),
        });
      }
    }
  }
  // one suggestion per table pair is enough (a table can reference another twice: keep the first)
  const seen = new Set<string>();
  return out.filter((j) => {
    const k = [j.left, j.right].sort().join("|");
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

// ------------------------------------------------------------------ SQL
export interface Issue { code: string; detail?: string }
export interface Generated {
  sql: string;
  /** :name parameters used in the criteria */
  params: string[];
  issues: Issue[];
}

const NUMBER = /^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i;
const PASS_THROUGH = /^(NULL|TRUE|FALSE|CURRENT_DATE|CURRENT_TIME|CURRENT_TIMESTAMP|LOCALTIME|LOCALTIMESTAMP|CURRENT_USER|CURRENT_ROLE|'TODAY'|'NOW')$/i;

/**
 * A criteria value as SQL: numbers, :params, ?, quoted strings, NULL/TRUE/FALSE/CURRENT_* and
 * anything wrapped in ( ) or prefixed with = (raw expression) pass through; other text is quoted.
 */
export function valueSql(raw: string): string {
  const v = raw.trim();
  if (!v) return "''";
  if (v.startsWith("=")) return v.slice(1).trim();
  if (NUMBER.test(v) || PASS_THROUGH.test(v) || /^:[A-Za-z_][A-Za-z0-9_$]*$/.test(v) || v === "?") return v;
  if (v.length >= 2 && v.startsWith("'") && v.endsWith("'") && !v.slice(1, -1).replace(/''/g, "").includes("'")) return v;
  if (v.startsWith("(") && v.endsWith(")")) return v;
  return `'${v.replace(/'/g, "''")}'`;
}

/** Split an IN list on commas outside quotes. */
export function splitList(raw: string): string[] {
  const out: string[] = [];
  let cur = "", q = false;
  for (const ch of raw) {
    if (ch === "'") q = !q;
    if (ch === "," && !q) { out.push(cur); cur = ""; } else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

function colRef(st: QbState, tableId: string, column: string): string | null {
  const t = st.tables.find((x) => x.id === tableId);
  if (!t) return null;
  return `${userIdent(t.alias || t.name)}.${quoteIdent(column)}`;
}

function aggWrap(agg: Agg, e: string): string {
  if (!agg) return e;
  if (agg === "COUNT DISTINCT") return `COUNT(DISTINCT ${e})`;
  return `${agg}(${e})`;
}

/** The expression of a field or criterion (without aggregate), or null when it points nowhere. */
function baseExpr(st: QbState, x: { table: string; column: string; expr: string }): string | null {
  if (x.table) return x.column === "*" ? `${userIdent(st.tables.find((t) => t.id === x.table)?.alias ?? "")}.*` : colRef(st, x.table, x.column);
  return x.expr.trim() || null;
}

export function fieldExpr(st: QbState, f: QbField): string | null {
  const e = baseExpr(st, f);
  if (e === null) return null;
  if (f.agg === "COUNT" && f.column === "*") return "COUNT(*)";
  return aggWrap(f.agg, e);
}

function criterionSql(st: QbState, c: QbCriterion, params: Set<string>): string | null {
  let e = baseExpr(st, c);
  if (e === null) return null;
  e = c.agg === "COUNT" && c.column === "*" ? "COUNT(*)" : aggWrap(c.agg, e);
  const note = (v: string) => {
    for (const m of v.matchAll(/(^|[^\w$:']):([A-Za-z_][A-Za-z0-9_$]*)/g)) params.add(m[2]);
    return v;
  };
  switch (c.op) {
    case "IS NULL":
    case "IS NOT NULL":
      return `${e} ${c.op}`;
    case "IN":
    case "NOT IN": {
      const v = c.value.trim();
      const list = /^\(.*\)$/s.test(v) ? v : `(${splitList(v).map(valueSql).join(", ")})`;
      return `${e} ${c.op} ${note(list)}`;
    }
    case "BETWEEN":
      return `${e} BETWEEN ${note(valueSql(c.value))} AND ${note(valueSql(c.value2))}`;
    default:
      return `${e} ${c.op} ${note(valueSql(c.value))}`;
  }
}

function joinCondition(st: QbState, j: QbJoin): string | null {
  if (j.custom.trim()) return j.custom.trim();
  const parts = j.conditions
    .filter((c) => c.left && c.right)
    .map((c) => `${colRef(st, j.left, c.left)} ${c.op || "="} ${colRef(st, j.right, c.right)}`);
  return parts.length ? parts.join(" AND ") : null;
}

const FLIP: Record<JoinType, JoinType> = { INNER: "INNER", LEFT: "RIGHT", RIGHT: "LEFT", FULL: "FULL" };

function fromClause(st: QbState, issues: Issue[]): string[] {
  const lines: string[] = [];
  const ref = (t: QbTable) => {
    const alias = t.alias.trim();
    return alias ? `${relationRef(t)} ${userIdent(alias)}` : relationRef(t);
  };
  if (!st.tables.length) return lines;
  const included = new Set<string>([st.tables[0].id]);
  lines.push(`FROM ${ref(st.tables[0])}`);
  // line index for each table introduced by a join (to AND extra conditions onto it)
  const introLine = new Map<string, number>();
  const pending = st.joins.filter((j) => st.tables.some((t) => t.id === j.left) && st.tables.some((t) => t.id === j.right) && j.left !== j.right);
  const used = new Set<string>();
  const byId = new Map(st.tables.map((t) => [t.id, t]));
  const place = () => {
    let progress = true;
    while (progress) {
      progress = false;
      for (const j of pending) {
        if (used.has(j.id)) continue;
        const l = included.has(j.left), r = included.has(j.right);
        if (!l && !r) continue;
        used.add(j.id);
        progress = true;
        const cond = joinCondition(st, j);
        if (l && r) {
          // both sides already in FROM: the condition joins the later of the two
          const line = Math.max(introLine.get(j.left) ?? -1, introLine.get(j.right) ?? -1);
          if (cond && line >= 0) lines[line] += ` AND ${cond}`;
          else if (cond) issues.push({ code: "join_ignored", detail: cond });
          continue;
        }
        const newId_ = l ? j.right : j.left;
        const type = l ? j.type : FLIP[j.type];
        if (!cond) issues.push({ code: "join_without_condition", detail: byId.get(newId_)!.name });
        lines.push(`${type === "INNER" ? "INNER" : type} JOIN ${ref(byId.get(newId_)!)} ON ${cond ?? "1 = 1"}`);
        introLine.set(newId_, lines.length - 1);
        included.add(newId_);
      }
    }
  };
  place();
  for (const t of st.tables) {
    if (included.has(t.id)) continue;
    issues.push({ code: "cross_join", detail: t.name });
    lines.push(`CROSS JOIN ${ref(t)}`);
    included.add(t.id);
    place();
  }
  return lines;
}

/** Generate the SELECT for ``st``. ``engineMajor`` picks the row-limit syntax available. */
export function generateSql(st: QbState, engineMajor = 5): Generated {
  const issues: Issue[] = [];
  const params = new Set<string>();
  if (!st.tables.length && !st.fields.some((f) => !f.table && f.expr.trim())) {
    return { sql: "", params: [], issues: [{ code: "no_tables" }] };
  }
  // aliases must be unique
  const aliases = st.tables.map((t) => (t.alias || t.name).toUpperCase());
  for (const a of new Set(aliases)) if (aliases.filter((x) => x === a).length > 1) issues.push({ code: "duplicate_alias", detail: a });

  const live = st.fields.filter((f) => fieldExpr(st, f) !== null);
  const outputs = live.filter((f) => f.output);
  const anyAgg = outputs.some((f) => f.agg) || st.criteria.some((c) => c.agg);
  const select: string[] = outputs.map((f) => {
    const e = fieldExpr(st, f)!;
    return f.alias.trim() ? `${e} AS ${userIdent(f.alias)}` : e;
  });
  if (!select.length) select.push(st.tables.length === 1 ? "*" : st.tables.map((t) => `${userIdent(t.alias || t.name)}.*`).join(", "));

  let head = "SELECT";
  let style = st.limitStyle;
  if (style === "fetch" && engineMajor < 3) style = "rows"; // OFFSET/FETCH needs Firebird 3
  const lim = st.limit != null && st.limit >= 0 ? Math.floor(st.limit) : null;
  const off = st.offset != null && st.offset > 0 ? Math.floor(st.offset) : null;
  if (style === "first" && (lim !== null || off !== null)) head += (lim !== null ? ` FIRST ${lim}` : "") + (off !== null ? ` SKIP ${off}` : "");
  if (st.distinct) head += " DISTINCT";

  const lines: string[] = [];
  lines.push(select.length > 1 ? `${head}\n  ${select.join(",\n  ")}` : `${head} ${select[0]}`);
  lines.push(...fromClause(st, issues));

  const where: QbCriterion[] = [], having: QbCriterion[] = [];
  for (const c of st.criteria) (c.agg ? having : where).push(c);
  const cond = (list: QbCriterion[]) => {
    const parts: string[] = [];
    list.forEach((c) => {
      const s = criterionSql(st, c, params);
      if (s === null) return;
      parts.push(parts.length ? `${c.connector} ${s}` : s);
    });
    // AND binds tighter than OR in SQL, which is what a criteria grid read top-down means
    return parts.join("\n  ");
  };
  const w = cond(where);
  if (w) lines.push(`WHERE ${w}`);
  if (anyAgg) {
    const groups = outputs.filter((f) => !f.agg && !(f.table && f.column === "*")).map((f) => fieldExpr(st, f)!);
    // ORDER BY columns that are not aggregated must be grouped too
    for (const f of live) if (!f.output && f.sort && !f.agg) groups.push(fieldExpr(st, f)!);
    const uniq = [...new Set(groups)];
    if (uniq.length) lines.push(`GROUP BY ${uniq.join(", ")}`);
    if (outputs.some((f) => f.table && f.column === "*" && !f.agg)) issues.push({ code: "star_with_aggregate" });
  }
  const h = cond(having);
  if (h) lines.push(`HAVING ${h}`);
  const order = live.filter((f) => f.sort).sort((a, b) => a.sortOrder - b.sortOrder);
  if (order.length) {
    if (st.distinct && order.some((f) => !f.output)) issues.push({ code: "distinct_order_not_selected" });
    lines.push(`ORDER BY ${order.map((f) => `${fieldExpr(st, f)} ${f.sort}`).join(", ")}`);
  }
  if (style === "rows" && (lim !== null || off !== null)) {
    const from = (off ?? 0) + 1;
    lines.push(lim !== null ? `ROWS ${from} TO ${(off ?? 0) + lim}` : `ROWS ${from} TO ${Number.MAX_SAFE_INTEGER}`);
    if (lim === 0) issues.push({ code: "rows_zero" });
  }
  if (style === "fetch" && (lim !== null || off !== null)) {
    if (off !== null) lines.push(`OFFSET ${off} ROWS`);
    if (lim !== null) lines.push(`FETCH NEXT ${lim} ROWS ONLY`);
  }
  return { sql: lines.join("\n"), params: [...params], issues };
}

// ------------------------------------------------------------------ saving the state with the SQL
const MARK = "-- fbadmin:query-builder ";

function b64encode(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin);
}
function b64decode(s: string): string {
  const bin = atob(s);
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

/** SQL followed by a comment line that carries the builder state (positions and all). */
export function withState(sql: string, st: QbState): string {
  const slim = { ...st, tables: st.tables.map((t) => ({ ...t, columns: [], foreign_keys: [] })) };
  return `${sql}\n${MARK}${b64encode(JSON.stringify(slim))}\n`;
}

/** The builder state saved in ``sql`` by withState, or null. Tables come back without columns. */
export function stateFromSql(sql: string): QbState | null {
  const i = sql.lastIndexOf(MARK);
  if (i < 0) return null;
  const data = sql.slice(i + MARK.length).split("\n")[0].trim();
  try {
    const st = JSON.parse(b64decode(data)) as QbState;
    if (st?.version !== 1 || !Array.isArray(st.tables)) return null;
    return { ...emptyState(), ...st };
  } catch {
    return null;
  }
}

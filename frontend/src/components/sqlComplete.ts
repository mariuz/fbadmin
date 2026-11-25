/**
 * Firebird-aware completion of table and column names (issues #2, #7, #8).
 *
 * - A name is quoted only when it has to be: it is not a regular identifier (letters, digits, `_` and `$`,
 *   starting with a letter, all upper case) or it is a reserved word. `MON$DATABASE` stays unquoted;
 *   `"MyTable"` (created quoted, mixed case) keeps its quotes.
 * - The word being completed includes `$`, so `mon$data` is replaced as a whole.
 * - Aliases are read from FROM and JOIN clauses; unquoted names fold to upper case, as Firebird does, so
 *   `from artikel a` resolves `a.` to the table ARTIKEL.
 */

/** Firebird 5 reserved words: a name equal to one of these must be quoted. */
export const RESERVED = new Set((
  "ADD ADMIN ALL ALTER AND ANY AS AT AVG BEGIN BETWEEN BIGINT BINARY BIT_LENGTH BLOB BOOLEAN BOTH BY CASE CAST " +
  "CHAR CHAR_LENGTH CHARACTER CHARACTER_LENGTH CHECK CLOSE COLLATE COLUMN COMMENT COMMIT CONNECT CONSTRAINT CORR " +
  "COUNT COVAR_POP COVAR_SAMP CREATE CROSS CURRENT CURRENT_CONNECTION CURRENT_DATE CURRENT_ROLE CURRENT_TIME " +
  "CURRENT_TIMESTAMP CURRENT_TRANSACTION CURRENT_USER CURSOR DATE DAY DEC DECFLOAT DECIMAL DECLARE DEFAULT DELETE " +
  "DELETING DETERMINISTIC DISCONNECT DISTINCT DOUBLE DROP ELSE END ESCAPE EXECUTE EXISTS EXTERNAL EXTRACT FALSE " +
  "FETCH FILTER FLOAT FOR FOREIGN FROM FULL FUNCTION GDSCODE GLOBAL GRANT GROUP HAVING HOUR IN INDEX INNER " +
  "INSENSITIVE INSERT INSERTING INT INT128 INTEGER INTO IS JOIN LATERAL LEADING LEFT LIKE LOCAL LOCALTIME " +
  "LOCALTIMESTAMP LONG LOWER MAX MERGE MIN MINUTE MONTH NATIONAL NATURAL NCHAR NO NOT NULL NUMERIC OCTET_LENGTH OF " +
  "OFFSET ON ONLY OPEN OR ORDER OUTER OVER PARAMETER PLAN POSITION POST_EVENT PRECISION PRIMARY PROCEDURE " +
  "PUBLICATION RDB$DB_KEY RDB$ERROR RDB$GET_CONTEXT RDB$GET_TRANSACTION_CN RDB$RECORD_VERSION RDB$ROLE_IN_USE " +
  "RDB$SET_CONTEXT RDB$SYSTEM_PRIVILEGE REAL RECORD_VERSION RECREATE RECURSIVE REFERENCES REGR_AVGX REGR_AVGY " +
  "REGR_COUNT REGR_INTERCEPT REGR_R2 REGR_SLOPE REGR_SXX REGR_SXY REGR_SYY RELEASE RESETTING RETURN " +
  "RETURNING_VALUES RETURNS REVOKE RIGHT ROLLBACK ROW ROW_COUNT ROWS SAVEPOINT SCROLL SECOND SELECT SENSITIVE SET " +
  "SIMILAR SMALLINT SOME SQLCODE SQLSTATE START STDDEV_POP STDDEV_SAMP SUM TABLE THEN TIME TIMESTAMP " +
  "TIMEZONE_HOUR TIMEZONE_MINUTE TO TRAILING TRIGGER TRIM TRUE UNBOUNDED UNION UNIQUE UNKNOWN UPDATE UPDATING " +
  "UPPER USER USING VALUE VALUES VAR_POP VAR_SAMP VARBINARY VARCHAR VARIABLE VARYING VIEW WHEN WHERE WHILE WINDOW " +
  "WITH WITHOUT YEAR"
).split(" "));

const REGULAR = /^[A-Z][A-Z0-9_$]*$/;

export function needsQuotes(name: string): boolean {
  return !REGULAR.test(name) || RESERVED.has(name);
}

/** The name as it must be written in SQL. */
export function sqlName(name: string): string {
  return needsQuotes(name) ? `"${name.replace(/"/g, '""')}"` : name;
}

/** The catalog name of an identifier as written: unquoted folds to upper case, "quoted" is taken as is. */
export function catalogName(written: string): string {
  const w = written.trim();
  if (w.startsWith('"') && w.endsWith('"') && w.length >= 2) return w.slice(1, -1).replace(/""/g, '"');
  return w.toUpperCase();
}

const IDENT = String.raw`(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)`;
const QNAME = String.raw`${IDENT}(?:\s*\.\s*${IDENT})?`;
/** Words that end a FROM item: never an alias. */
const NOT_ALIAS = new Set(("WHERE JOIN INNER LEFT RIGHT FULL OUTER CROSS NATURAL ON USING GROUP ORDER HAVING UNION " +
  "PLAN ROWS FETCH OFFSET FOR WITH WINDOW AS SET INTO VALUES RETURNING LATERAL").split(" "));

function lastPart(qname: string): string {
  const parts = qname.match(new RegExp(IDENT, "g")) ?? [qname];
  return parts[parts.length - 1];
}

/** Aliases (and table names) of the FROM and JOIN items: alias (catalog form) -> table (catalog form). */
export function aliases(sql: string): Record<string, string> {
  const out: Record<string, string> = {};
  const text = sql.replace(/--[^\n]*|\/\*[\s\S]*?\*\/|'(?:[^']|'')*'/g, " ");
  const item = new RegExp(String.raw`(${QNAME})(?:\s+(?:AS\s+)?(${IDENT}))?`, "iy");
  const start = /\b(FROM|JOIN|UPDATE|INTO)\s+/gi;
  for (const m of text.matchAll(start)) {
    let pos = (m.index ?? 0) + m[0].length;
    // FROM a x, b y: a comma-separated list after FROM
    for (;;) {
      item.lastIndex = pos;
      const it = item.exec(text);
      if (!it) break;
      const table = catalogName(lastPart(it[1]));
      let alias: string | undefined = it[2];
      if (alias && !alias.startsWith('"') && NOT_ALIAS.has(alias.toUpperCase())) alias = undefined;
      out[table] = out[table] ?? table;
      if (alias) out[catalogName(alias)] = table;
      pos = item.lastIndex - (it[2] && !alias ? it[2].length : 0);
      const comma = /\s*,\s*/y;
      comma.lastIndex = pos;
      if (m[1].toUpperCase() !== "FROM" || !comma.exec(text)) break;
      pos = comma.lastIndex;
    }
  }
  return out;
}

/** The word before ``pos``: where it starts, whether it is quoted, and its qualifier (`a.` in `a.co`). */
export function wordAt(text: string, pos: number): { from: number; word: string; quoted: boolean; qualifier: string | null } {
  let i = pos;
  let quoted = false;
  // inside an open quote: "MON$DA|
  const open = text.lastIndexOf('"', pos - 1);
  if (open >= 0 && /^[^"\s]*$/.test(text.slice(open + 1, pos)) && (text.slice(0, open).match(/"/g)?.length ?? 0) % 2 === 0) {
    i = open;
    quoted = true;
  } else {
    while (i > 0 && /[A-Za-z0-9_$]/.test(text[i - 1])) i--;
  }
  let qualifier: string | null = null;
  if (text[i - 1] === ".") {
    const before = text.slice(0, i - 1);
    const q = new RegExp(`(${IDENT})\\s*$`).exec(before);
    if (q) qualifier = catalogName(q[1]);
  }
  return { from: i, word: text.slice(i, pos), quoted, qualifier };
}

export interface NameOption { label: string; apply: string; type: string; detail?: string }

/** Completion options for the word at ``pos``: columns after `alias.`, otherwise tables and aliases. */
export function nameOptions(text: string, pos: number, schema: Record<string, string[]>):
  { from: number; quoted: boolean; options: NameOption[] } | null {
  const w = wordAt(text, pos);
  const quote = (n: string) => (w.quoted ? `"${n.replace(/"/g, '""')}"` : sqlName(n));
  if (w.qualifier) {
    const map = aliases(text);
    const table = map[w.qualifier] ?? w.qualifier;
    const cols = schema[table];
    if (!cols) return null;
    return { from: w.from, quoted: w.quoted, options: cols.map((c) => ({ label: c, apply: quote(c), type: "property", detail: table })) };
  }
  const options: NameOption[] = Object.keys(schema).map((tbl) => ({ label: tbl, apply: quote(tbl), type: "type" }));
  for (const [alias, table] of Object.entries(aliases(text))) {
    if (alias !== table) options.push({ label: alias, apply: quote(alias), type: "constant", detail: table });
  }
  return { from: w.from, quoted: w.quoted, options };
}

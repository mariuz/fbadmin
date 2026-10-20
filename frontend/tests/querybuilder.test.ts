// Unit tests of the query builder's SQL generation: `npm test` (node --test, no extra dependencies).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  emptyState, generateSql, quoteIdent, setReservedWords, stateFromSql, suggestAlias, suggestJoins, valueSql, withState,
  type QbField, type QbState, type QbTable,
} from "../src/features/querybuilder/model.ts";

const customer: QbTable = {
  id: "c", schema: null, name: "CUSTOMER", kind: "table", alias: "C", x: 0, y: 0, foreign_keys: [],
  columns: [{ name: "ID", type: "INTEGER", pk: true }, { name: "NAME", type: "VARCHAR(50)" }, { name: "CITY", type: "VARCHAR(30)" }],
};
const orders: QbTable = {
  id: "o", schema: null, name: "ORDERS", kind: "table", alias: "O", x: 0, y: 0,
  foreign_keys: [{ name: "FK_O_C", columns: ["CUSTOMER_ID"], ref_table: "CUSTOMER", ref_columns: ["ID"] }],
  columns: [{ name: "ID", type: "INTEGER" }, { name: "CUSTOMER_ID", type: "INTEGER", fk: true }, { name: "AMOUNT", type: "NUMERIC(12,2)" }, { name: "POSITION", type: "INTEGER" }],
};
let n = 0;
const field = (p: Partial<QbField>): QbField => ({ id: `f${n++}`, table: "", column: "", expr: "", agg: "", alias: "", output: true, sort: "", sortOrder: 0, ...p });
const st = (p: Partial<QbState>): QbState => ({ ...emptyState(), ...p });

test("quoting follows the backend rule, with the server's reserved words", () => {
  assert.equal(quoteIdent("NAME"), "NAME");
  assert.equal(quoteIdent("POSITION"), '"POSITION"');
  assert.equal(quoteIdent("lower"), '"lower"');
  assert.equal(quoteIdent('A"B'), '"A""B"');
  setReservedWords(["NAME"]);
  assert.equal(quoteIdent("NAME"), '"NAME"');
  setReservedWords(["POSITION", "SELECT", "ORDER", "AS", "TO"]);
});

test("single table without fields selects *", () => {
  assert.equal(generateSql(st({ tables: [customer] })).sql, "SELECT *\nFROM CUSTOMER C");
});

test("aliases are short, unique and not reserved", () => {
  assert.equal(suggestAlias("ORDER_LINES", []), "OL");
  assert.equal(suggestAlias("ORDER_LINES", ["OL"]), "OL2");
  assert.equal(suggestAlias("AUDIT_SUMMARY", []), "AS2"); // AS is reserved
});

test("joins are suggested from foreign keys in both directions", () => {
  const a = suggestJoins(orders, [customer]);
  assert.equal(a.length, 1);
  assert.deepEqual([a[0].left, a[0].right, a[0].conditions], ["c", "o", [{ left: "ID", op: "=", right: "CUSTOMER_ID" }]]);
  const b = suggestJoins(customer, [orders]);
  assert.deepEqual([b[0].left, b[0].right, b[0].conditions], ["o", "c", [{ left: "CUSTOMER_ID", op: "=", right: "ID" }]]);
  assert.equal(suggestJoins(orders, [{ ...customer, schema: "APP" }]).length, 0);
});

test("join, aggregate with automatic GROUP BY, WHERE, HAVING, ORDER BY, OFFSET/FETCH", () => {
  const [j] = suggestJoins(orders, [customer]);
  j.type = "LEFT";
  const s = st({
    tables: [customer, orders], joins: [j],
    fields: [
      field({ table: "c", column: "NAME", sort: "ASC", sortOrder: 2 }),
      field({ table: "o", column: "ID", agg: "COUNT", alias: "n" }),
      field({ table: "o", column: "AMOUNT", agg: "SUM", alias: "total", sort: "DESC", sortOrder: 1 }),
      field({ table: "o", column: "POSITION", agg: "MAX" }),
    ],
    criteria: [
      { id: "1", table: "c", column: "CITY", expr: "", agg: "", op: "=", value: "Rome", value2: "", connector: "AND" },
      { id: "2", table: "c", column: "ID", expr: "", agg: "", op: "IN", value: "1, 2,'x,y'", value2: "", connector: "OR" },
      { id: "3", table: "o", column: "AMOUNT", expr: "", agg: "SUM", op: ">", value: ":minimum", value2: "", connector: "AND" },
    ],
    limit: 10, offset: 20,
  });
  const g = generateSql(s, 5);
  assert.equal(g.sql, [
    "SELECT",
    "  C.NAME,",
    "  COUNT(O.ID) AS N,",
    "  SUM(O.AMOUNT) AS TOTAL,",
    '  MAX(O."POSITION")',
    "FROM CUSTOMER C",
    "LEFT JOIN ORDERS O ON C.ID = O.CUSTOMER_ID",
    "WHERE C.CITY = 'Rome'",
    "  OR C.ID IN (1, 2, 'x,y')",
    "GROUP BY C.NAME",
    "HAVING SUM(O.AMOUNT) > :minimum",
    "ORDER BY SUM(O.AMOUNT) DESC, C.NAME ASC",
    "OFFSET 20 ROWS",
    "FETCH NEXT 10 ROWS ONLY",
  ].join("\n"));
  assert.deepEqual(g.params, ["minimum"]);
  assert.deepEqual(g.issues, []);
});

test("limit syntaxes: FIRST/SKIP and ROWS", () => {
  const base = { tables: [customer], fields: [field({ table: "c", column: "ID" })], limit: 5, offset: 10 };
  assert.equal(generateSql(st({ ...base, limitStyle: "first", distinct: true })).sql, "SELECT FIRST 5 SKIP 10 DISTINCT C.ID\nFROM CUSTOMER C");
  assert.equal(generateSql(st({ ...base, limitStyle: "rows" })).sql, "SELECT C.ID\nFROM CUSTOMER C\nROWS 11 TO 15");
  assert.equal(generateSql(st({ ...base, limitStyle: "fetch" }), 2).sql, "SELECT C.ID\nFROM CUSTOMER C\nROWS 11 TO 15");
});

test("joins written from the right table are flipped; unjoined tables become CROSS JOIN", () => {
  const [j] = suggestJoins(customer, [orders]); // left = orders
  j.type = "LEFT";
  const g = generateSql(st({ tables: [customer, orders], joins: [j] }));
  assert.match(g.sql, /RIGHT JOIN ORDERS O ON O.CUSTOMER_ID = C.ID/);
  const x = generateSql(st({ tables: [customer, orders] }));
  assert.match(x.sql, /CROSS JOIN ORDERS O/);
  assert.deepEqual(x.issues, [{ code: "cross_join", detail: "ORDERS" }]);
});

test("Firebird 6 schema-qualified names", () => {
  const g = generateSql(st({ tables: [{ ...customer, schema: "APP" }, { ...orders, schema: "my schema" }] }));
  assert.match(g.sql, /FROM APP.CUSTOMER C/);
  assert.match(g.sql, /CROSS JOIN "my schema".ORDERS O/);
});

test("criteria values", () => {
  assert.equal(valueSql("42"), "42");
  assert.equal(valueSql("-1.5e3"), "-1.5e3");
  assert.equal(valueSql("O'Brien"), "'O''Brien'");
  assert.equal(valueSql("'ok'"), "'ok'");
  assert.equal(valueSql("'it''s'"), "'it''s'");
  assert.equal(valueSql("'a'b'"), "'''a''b'''");
  assert.equal(valueSql(":p"), ":p");
  assert.equal(valueSql("null"), "null");
  assert.equal(valueSql("=CURRENT_DATE - 7"), "CURRENT_DATE - 7");
  assert.equal(valueSql("2024-01-31"), "'2024-01-31'");
});

test("builder state survives a round trip through the SQL comment", () => {
  const s = st({ tables: [customer], fields: [field({ table: "c", column: "NAME", alias: "Név" })] });
  const sql = withState(generateSql(s).sql, s);
  const back = stateFromSql(sql)!;
  assert.equal(back.tables[0].name, "CUSTOMER");
  assert.equal(back.fields[0].alias, "Név");
  assert.deepEqual(back.tables[0].columns, []);
  assert.equal(stateFromSql("SELECT 1 FROM RDB$DATABASE"), null);
});

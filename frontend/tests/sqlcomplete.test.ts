// Completion of Firebird names (issues #2, #7, #8): `npm test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { aliases, catalogName, nameOptions, needsQuotes, sqlName, wordAt } from "../src/components/sqlComplete.ts";

const schema = { ARTIKEL: ["ARTIKELID", "NAME", "VALUE"], "MON$DATABASE": ["MON$PAGE_SIZE"], MyTable: ["Id", "X"] };
const apply = (sql: string, pos = sql.length) => {
  const r = nameOptions(sql, pos, schema);
  return r && Object.fromEntries(r.options.map((o) => [o.label, o.apply]));
};

test("quote only when needed (#8)", () => {
  assert.equal(sqlName("ARTIKEL"), "ARTIKEL");
  assert.equal(sqlName("MON$DATABASE"), "MON$DATABASE");
  assert.equal(sqlName("MyTable"), '"MyTable"');
  assert.equal(sqlName("VALUE"), '"VALUE"');      // reserved
  assert.equal(sqlName("MY TABLE"), '"MY TABLE"');
  assert.equal(sqlName('A"B'), '"A""B"');
  assert.equal(sqlName("_X"), '"_X"');             // a regular identifier starts with a letter
  assert.ok(!needsQuotes("NAME"));                  // a non-reserved keyword is fine
  assert.equal(apply("select * from art")!.ARTIKEL, "ARTIKEL");
  assert.equal(apply('select * from "art')!.ARTIKEL, '"ARTIKEL"'); // typed a quote: stays quoted
});

test("the whole prefix with $ is replaced (#2)", () => {
  const sql = "select * from mon$data";
  const r = nameOptions(sql, sql.length, schema)!;
  assert.equal(r.from, "select * from ".length);
  assert.equal(apply(sql)!["MON$DATABASE"], "MON$DATABASE");
  assert.deepEqual(wordAt("x mon$da", 8), { from: 2, word: "mon$da", quoted: false, qualifier: null });
});

test("aliases of unquoted tables resolve (#7)", () => {
  assert.deepEqual(aliases("select a. from artikel a"), { ARTIKEL: "ARTIKEL", A: "ARTIKEL" });
  assert.equal(catalogName("artikel"), "ARTIKEL");
  assert.equal(catalogName('"MyTable"'), "MyTable");
  const sql = "select a. from artikel a";
  const cols = apply(sql, "select a.".length)!;
  assert.deepEqual(Object.keys(cols), ["ARTIKELID", "NAME", "VALUE"]);
  assert.equal(cols.VALUE, '"VALUE"');
  assert.ok(apply('select a. from "ARTIKEL" a', "select a.".length)!.ARTIKELID);
  assert.ok(apply("select a.na from artikel as a where a.artikelid = 1", "select a.na".length)!.NAME);
  // FROM lists, joins, schema-qualified names, quoted aliases, keywords that are not aliases
  assert.deepEqual(aliases('select 1 from artikel a, "MyTable" m join mon$database d on 1=1 where x = 1'),
    { ARTIKEL: "ARTIKEL", A: "ARTIKEL", MyTable: "MyTable", M: "MyTable", "MON$DATABASE": "MON$DATABASE", D: "MON$DATABASE" });
  assert.deepEqual(aliases("select * from public.artikel where 1=1"), { ARTIKEL: "ARTIKEL" });
  assert.equal(apply('select m. from "MyTable" m', "select m.".length)!.Id, '"Id"');
  // the table name itself works as a qualifier
  assert.ok(apply("select artikel. from artikel", "select artikel.".length)!.NAME);
  // aliases are offered too
  assert.equal(apply("select  from artikel a", "select ".length)!.A, "A");
});

test("unknown qualifier: no options", () => {
  assert.equal(nameOptions("select z. from artikel a", "select z.".length, schema), null);
});

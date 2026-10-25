import { test } from "node:test";
import assert from "node:assert/strict";
import { referencedNames } from "../src/components/sqlNames.ts";

test("names referenced in SQL: unquoted upper-cased, quoted as written", () => {
  const n = referencedNames('select c.name from customers c join "Order Lines" ol on ol.id = c.id where x = \'t1\'');
  for (const name of ["SELECT", "C", "NAME", "CUSTOMERS", "Order Lines", "OL", "ID"]) assert.ok(n.has(name), name);
  assert.ok(!n.has("customers"));
  assert.ok(referencedNames('"a""b"').has('a"b'));
});

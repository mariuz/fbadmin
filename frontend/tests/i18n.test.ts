// The catalogs and keys with a context (``"Line@@chart"``): `npm test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { untranslated } from "../src/i18n-context.ts";

const dir = new URL("../src/locales/", import.meta.url);
const catalogs = Object.fromEntries(readdirSync(dir).filter((f) => f.endsWith(".json"))
  .map((f) => [f.replace(".json", ""), JSON.parse(readFileSync(new URL(f, dir), "utf-8")) as Record<string, string>]));

test("a key's context is dropped in English", () => {
  assert.equal(untranslated("Line@@chart"), "Line");
  assert.equal(untranslated("Revoke@@token"), "Revoke");
  assert.equal(untranslated("Line"), "Line");
  assert.equal(untranslated("a@@b c"), "a@@b c"); // only a trailing word is a context
});

test("words with two meanings are translated apart", () => {
  for (const [lang, cat] of Object.entries(catalogs)) {
    for (const [plain, ctx] of [["Line", "Line@@chart"], ["Revoke", "Revoke@@token"]]) {
      assert.ok(cat[ctx], `${lang}: ${ctx} is translated`);
    }
    // the chart type is not "line number" (the clash this fixes in cs, de, ru)
    if (["cs", "de", "ru"].includes(lang)) {
      assert.notEqual(cat["Line@@chart"], cat["Line"], `${lang}: chart type`);
    }
    // "Revoke" of a role member reads "remove the privilege" in cs and de; a token is revoked differently
    // (in Russian "Отозвать" fits both)
    if (["cs", "de"].includes(lang)) assert.notEqual(cat["Revoke@@token"], cat["Revoke"], `${lang}: token`);
  }
});

test("no translation keeps a context marker", () => {
  for (const [lang, cat] of Object.entries(catalogs)) {
    for (const [k, v] of Object.entries(cat)) assert.ok(!v.includes("@@"), `${lang}: ${k} -> ${v}`);
  }
});

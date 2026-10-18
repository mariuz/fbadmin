#!/usr/bin/env node
// Collect t("...") strings from src/ and sync src/locales/<lang>.json catalogs.
// Usage: node scripts-i18n.mjs [--check]   (--check exits 1 when catalogs miss keys)
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const LANGS = { ro: "Română", "pt-BR": "Português (Brasil)", es: "Español", ru: "Русский", de: "Deutsch", fr: "Français", cs: "Čeština" };
const root = new URL("./src/", import.meta.url).pathname;
const files = [];
(function walk(d) {
  for (const f of readdirSync(d)) {
    const p = join(d, f);
    if (statSync(p).isDirectory()) { if (f !== "locales") walk(p); }
    else if (/\.(tsx?|jsx?)$/.test(f)) files.push(p);
  }
})(root);

const keys = new Set();
const re = /\b(?:t|N_)\(\s*(["'`])((?:\\.|(?!\1).)*)\1/g;
for (const f of files) {
  const src = readFileSync(f, "utf8");
  for (const m of src.matchAll(re)) if (!m[2].includes("${")) keys.add(m[2].replace(/\\(["'`\\])/g, "$1"));
}
const sorted = [...keys].sort();
let missing = 0;
for (const [code, name] of Object.entries(LANGS)) {
  const path = join(root, "locales", `${code}.json`);
  const cur = existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : {};
  const out = { "@name": cur["@name"] ?? name };
  for (const k of sorted) {
    out[k] = cur[k] ?? "";
    if (!out[k]) missing++;
  }
  if (!process.argv.includes("--check")) writeFileSync(path, JSON.stringify(out, null, 2) + "\n");
}
console.log(`${sorted.length} strings, ${missing} untranslated entries across ${Object.keys(LANGS).length} languages`);
if (process.argv.includes("--check") && missing) process.exit(1);

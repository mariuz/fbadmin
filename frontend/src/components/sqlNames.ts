/** Table names referenced in SQL text: unquoted identifiers upper-cased, "quoted" ones as written. */
export function referencedNames(sql: string): Set<string> {
  const out = new Set<string>();
  for (const m of sql.matchAll(/"((?:[^"]|"")+)"|([A-Za-z_][A-Za-z0-9_$]*)/g)) {
    out.add(m[1] !== undefined ? m[1].replace(/""/g, "\"") : m[2].toUpperCase());
  }
  return out;
}

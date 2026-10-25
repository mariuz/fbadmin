import { useEffect, useRef } from "react";
import { request } from "../api";
import { referencedNames } from "./sqlNames";

/**
 * Very large schemas (completion sent with ``lazy_columns``): the editor knows every table name, and the
 * columns of the tables a query mentions are fetched on demand (debounced) and merged into the schema.
 */
export function useLazyColumns(sid: string, dbid: string, sql: string, lazy: boolean,
  schema: Record<string, string[]>, setSchema: (f: (s: Record<string, string[]>) => Record<string, string[]>) => void) {
  const asked = useRef(new Set<string>());
  const current = useRef(schema);
  current.current = schema;
  useEffect(() => {
    if (!lazy) return;
    const timer = setTimeout(() => {
      const known = current.current;
      const want = [...referencedNames(sql)].filter((n) => n in known && !asked.current.has(n)).slice(0, 200);
      if (!want.length) return;
      want.forEach((n) => asked.current.add(n));
      request<{ relations: Record<string, string[]> }>("POST", `/api/servers/${sid}/databases/${dbid}/completion/columns`,
        { relations: want })
        .then((r) => setSchema((s) => ({ ...s, ...r.relations })))
        .catch(() => want.forEach((n) => asked.current.delete(n)));
    }, 400);
    return () => clearTimeout(timer);
  }, [sql, lazy]);
}

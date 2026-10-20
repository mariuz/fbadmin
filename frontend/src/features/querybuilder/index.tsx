/** Visual query builder (milestone 2.0): a tab per database, opened from the database, table and view menus. */
import "./querybuilder.css";
import type { AppApi } from "../../context";
import type { Selection } from "../../types";
import { registerMenu, registerTabKind } from "../../registry";
import { t } from "../../i18n";
import { QueryBuilderTab } from "./QueryBuilderTab";

export const IconQueryBuilder = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="#2563eb" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="2" y="3" width="8" height="8" rx="1" /><rect x="14" y="13" width="8" height="8" rx="1" /><path d="M10 7h4v6M5 15v4h5" />
  </svg>
);

registerTabKind("query-builder", {
  icon: <IconQueryBuilder size={13} />,
  render: (tab, visible) => <QueryBuilderTab tab={tab} visible={visible} />,
  // one builder per database (and schema); opening it again for a table adds that table
  same: (a, b) => a.serverId === b.serverId && a.dbId === b.dbId && a.schema === b.schema,
});

export function openQueryBuilder(app: AppApi, sid: string, dbid: string, table?: { name: string; kind: "table" | "view"; schema?: string }) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
  app.openTab({
    kind: "query-builder", title: `${db?.name ?? t("database")} · ${t("query builder")}`, serverId: sid, dbId: dbid,
    props: table ? { table: table.name, kind: table.kind, schema: table.schema, nonce: Date.now() } : undefined,
  });
}

registerMenu((app: AppApi, sel: Selection) => {
  if (!sel.dbId) return [];
  if (sel.kind === "database") {
    return [{ label: t("Query builder"), icon: <IconQueryBuilder />, onClick: () => openQueryBuilder(app, sel.serverId, sel.dbId!) }];
  }
  if (sel.kind === "object" && (sel.otype === "tables" || sel.otype === "views")) {
    return [{
      label: t("Add to query builder"), icon: <IconQueryBuilder />,
      onClick: () => openQueryBuilder(app, sel.serverId, sel.dbId!, { name: sel.name!, kind: sel.otype === "views" ? "view" : "table", schema: sel.schema }),
    }];
  }
  return [];
});

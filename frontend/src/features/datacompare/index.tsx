/**
 * Milestone 1.2 — data compare: compare the rows of tables in two databases by primary key and
 * build a sync script (INSERT / UPDATE / DELETE) that makes the target equal to the source.
 */
import "./datacompare.css";
import type { AppApi } from "../../context";
import type { Selection } from "../../types";
import { registerMenu, registerTabKind } from "../../registry";
import type { MenuItem } from "../../components/ContextMenu";
import { N_, t } from "../../i18n";
import { DataCompareTab } from "./DataCompareTab";

// messages sent by the backend (translated where they are shown)
N_("Only in the target database"); N_("Only in the source database"); N_("Global temporary table");
N_("No primary key or unique constraint on columns present in both databases");
N_("The key is not unique in the target database"); N_("The key is not unique in the source database");
N_("No primary key: compared by a unique key (rows with NULL in the key are skipped)");
N_("Key columns cannot be ignored"); N_("pending"); N_("running");

const IconDataDiff = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="#0d9488" strokeWidth={1.8} strokeLinecap="round" aria-hidden="true">
    <rect x="3" y="4" width="7" height="16" rx="1" /><rect x="14" y="4" width="7" height="16" rx="1" /><path d="M3 9h7M3 14h7M14 9h7M14 14h7" />
  </svg>
);

registerTabKind("data-compare", { icon: <IconDataDiff size={13} />, render: (tab) => <DataCompareTab tab={tab} />, same: () => false });

function open(app: AppApi, sid: string, dbid: string, tables?: string[]) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
  const title = tables?.length === 1 ? t("compare {table}", { table: tables[0] }) : t("data compare");
  app.openTab({ kind: "data-compare", title: `${db?.name ?? t("database")} · ${title}`, serverId: sid, dbId: dbid,
    props: tables ? { tables } : undefined });
}

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (!sel.dbId) return [];
  if (sel.kind === "database")
    return [{ label: t("Compare data…"), icon: <IconDataDiff />, onClick: () => open(app, sel.serverId, sel.dbId!) }];
  if (sel.kind === "object" && sel.otype === "tables" && sel.name)
    return [{ label: t("Compare data with another database…"), icon: <IconDataDiff />, onClick: () => open(app, sel.serverId, sel.dbId!, [sel.name!]) }];
  return [];
});

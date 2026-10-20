/**
 * Schema history in git (milestone 2.0): the database's DDL committed to a git repository, one file
 * per object, with a commit log, per-object diffs and migration scripts between snapshots.
 */
import "./history.css";
import type { AppApi } from "../../context";
import type { Selection } from "../../types";
import { registerMenu, registerTabKind } from "../../registry";
import type { MenuItem } from "../../components/ContextMenu";
import { HistoryTab } from "./HistoryTab";
import { t } from "../../i18n";

const IconHistory = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="6" cy="6" r="2.2" /><circle cx="6" cy="18" r="2.2" /><circle cx="18" cy="9" r="2.2" /><path d="M6 8.2v7.6M18 11.2c0 3-3 3.8-9.8 5.6" />
  </svg>
);

registerTabKind("schema-history", { icon: <IconHistory size={13} />, render: (tab) => <HistoryTab tab={tab} /> });

export function openSchemaHistory(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "schema-history", title: t("{db} · schema history", { db }), serverId: sid, dbId: dbid });
}

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Schema history (git)…"), icon: <IconHistory />, onClick: () => openSchemaHistory(app, sid, dbid) }];
});

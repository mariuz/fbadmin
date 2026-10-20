/**
 * Upgrade assistant (milestone 1.2): checks a database before it moves to a newer Firebird
 * version, optionally with a trial restore on the target server, and writes a migration plan.
 */
import "./upgrade.css";
import type { AppApi } from "../../context";
import type { Selection } from "../../types";
import { registerMenu, registerTabKind } from "../../registry";
import type { MenuItem } from "../../components/ContextMenu";
import { t } from "../../i18n";
import { UpgradeTab } from "./UpgradeTab";

const IconUpgrade = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="#2e7d32" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 19V5M5 12l7-7 7 7" /><path d="M5 21h14" />
  </svg>
);

registerTabKind("upgrade", { icon: <IconUpgrade size={13} />, render: (tab) => <UpgradeTab tab={tab} /> });

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  const name = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  return [
    { label: t("Upgrade assistant…"), icon: <IconUpgrade />, onClick: () =>
      app.openTab({ kind: "upgrade", title: `${name} · ${t("upgrade")}`, serverId: sid, dbId: dbid, props: {} }) },
  ];
});

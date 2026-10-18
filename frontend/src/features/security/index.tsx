/**
 * Security feature (milestone 0.4): user menu (master password, sign out), fbadmin user admin,
 * audit log, Firebird users/roles/mappings per database and Services API users per server.
 * The login page and auth error handling live in AuthGate (wrapped around <App/> in main.tsx).
 */
import { registerMenu, registerTabKind, registerTopbar } from "../../registry";
import { IconRole, IconUser } from "../../components/Icons";
import { AuditLog, UsersAdmin } from "./AdminPanels";
import { FbSecurityPanel, SvcUsersPanel } from "./FbSecurity";
import { UserMenu } from "./UserMenu";
import "./security.css";
import { t } from "../../i18n";

registerTopbar({ id: "security-user", render: (app) => <UserMenu app={app} /> });

registerTabKind("security-users", { icon: <IconUser size={13} />, render: () => <UsersAdmin />, same: (a, b) => a.kind === b.kind });
registerTabKind("security-audit", {
  icon: <IconRole size={13} />,
  render: (_t, visible) => <AuditLog visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});
registerTabKind("fb-security", {
  icon: <IconRole size={13} />,
  render: (tab) => <FbSecurityPanel serverId={tab.serverId!} dbId={tab.dbId!} />,
});
registerTabKind("fb-svc-users", {
  icon: <IconUser size={13} />,
  render: (tab) => <SvcUsersPanel serverId={tab.serverId!} />,
  same: (a, b) => a.kind === b.kind && a.serverId === b.serverId,
});

registerMenu((app, sel) => {
  const srv = app.servers.find((s) => s.id === sel.serverId);
  if (!srv) return [];
  if (sel.kind === "server") {
    return [{
      label: t("Firebird users (Services API)…"), icon: <IconUser />,
      onClick: () => app.openTab({ kind: "fb-svc-users", title: t("{server} · users", { server: srv.name }), serverId: srv.id }),
    }];
  }
  const showDb = sel.kind === "database" || (sel.kind === "folder" && (sel.otype === "users" || sel.otype === "roles"));
  if (showDb && sel.dbId) {
    const db = srv.databases.find((d) => d.id === sel.dbId);
    return [{
      label: t("Users, roles & mappings…"), icon: <IconRole />,
      onClick: () => app.openTab({ kind: "fb-security", title: t("{db} · security", { db: db?.name ?? "db" }), serverId: srv.id, dbId: sel.dbId }),
    }];
  }
  return [];
});

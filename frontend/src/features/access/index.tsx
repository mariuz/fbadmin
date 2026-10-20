/**
 * Milestone 2.0 — server-mode security: two-factor sign-in (TOTP), personal API tokens and finer
 * permissions (per server, database and object). The sign-in step lives in Mfa.tsx and is used by
 * the login page (features/security/AuthGate.tsx).
 */
import { registerTabKind } from "../../registry";
import { IconKey, IconRole } from "../../components/Icons";
import { t } from "../../i18n";
import { registerUserMenuItem } from "../security/state";
import { AccountPanel } from "./Account";
import { AdminPanel } from "./Admin";
import "./access.css";

registerTabKind("access-account", {
  icon: <IconKey size={13} />,
  render: (_tab, visible) => <AccountPanel visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});
registerTabKind("access-admin", {
  icon: <IconRole size={13} />,
  render: (_tab, visible) => <AdminPanel visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});

registerUserMenuItem({
  id: "access-account",
  label: () => t("Sign-in & API tokens…"),
  show: (s) => s.server_mode,
  run: (app) => app.openTab({ kind: "access-account", title: t("Sign-in & API tokens") }),
});
registerUserMenuItem({
  id: "access-admin",
  admin: true,
  label: () => t("Permissions & two-factor"),
  show: (s) => s.server_mode,
  run: (app) => app.openTab({ kind: "access-admin", title: t("Permissions") }),
});

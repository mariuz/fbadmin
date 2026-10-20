/**
 * Milestone 1.2 — PSQL debugging help: trace runs (an instrumented copy of a procedure, function or
 * EXECUTE BLOCK records the line and the variables after each statement), per-line timings from the
 * Firebird 5 profiler, and saved test cases. Every run is rolled back.
 */
import { registerMenu, registerTabKind } from "../../registry";
import type { AppApi } from "../../context";
import { IconPlay } from "../../components/Icons";
import { t } from "../../i18n";
import { DebugTab } from "./Debugger";
import "./debugger.css";

export function openDebugger(app: AppApi, sid: string, dbid: string, otype: string | null, name: string | null, view: "trace" | "tests") {
  const title = otype === "block" ? t("Debug EXECUTE BLOCK") : name ? (view === "tests" ? t("Tests: {name}", { name }) : t("Debug: {name}", { name }))
    : t("PSQL tests");
  app.openTab({ kind: "psql-debug", title, serverId: sid, dbId: dbid, name: name ?? undefined, props: { otype, name, view, at: Date.now() } });
}

registerMenu((app, sel) => {
  if (!sel.dbId) return [];
  if (sel.kind === "object" && (sel.otype === "procedures" || sel.otype === "functions") && sel.name) {
    return [
      { label: t("Debug / trace…"), icon: <IconPlay />, onClick: () => openDebugger(app, sel.serverId, sel.dbId!, sel.otype!, sel.name!, "trace") },
      { label: t("Tests…"), onClick: () => openDebugger(app, sel.serverId, sel.dbId!, sel.otype!, sel.name!, "tests") },
    ];
  }
  if (sel.kind === "database") {
    return [
      { label: t("Debug EXECUTE BLOCK…"), onClick: () => openDebugger(app, sel.serverId, sel.dbId!, "block", null, "trace") },
      { label: t("PSQL tests…"), onClick: () => openDebugger(app, sel.serverId, sel.dbId!, null, null, "tests") },
    ];
  }
  return [];
});

registerTabKind("psql-debug", {
  icon: <IconPlay size={13} />,
  render: (tab, visible) => <DebugTab tab={tab} visible={visible} />,
  same: (a, b) => a.serverId === b.serverId && a.dbId === b.dbId && (a.props?.otype ?? null) === (b.props?.otype ?? null)
    && (a.props?.name ?? null) === (b.props?.name ?? null),
});

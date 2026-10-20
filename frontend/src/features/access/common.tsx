import { N_, t } from "../../i18n";
import type { Level } from "./api";

export const LEVEL_LABELS: Record<Level, string> = {
  none: N_("no access"),
  read: N_("read"),
  write: N_("write (DML)"),
  ddl: N_("DDL"),
  admin: N_("admin tools"),
};

export const LEVEL_HELP: Record<Level, string> = {
  none: N_("hidden"),
  read: N_("queries, metadata, monitoring"),
  write: N_("read + INSERT, UPDATE, DELETE, procedures"),
  ddl: N_("write + CREATE, ALTER, DROP, GRANT"),
  admin: N_("DDL + backup, restore, sweep, shutdown, Firebird users, trace"),
};

export const LEVELS: Level[] = ["none", "read", "write", "ddl", "admin"];

export function LevelPill(props: { level: Level }) {
  return <span className={"pill level-" + props.level} title={t(LEVEL_HELP[props.level])}>{t(LEVEL_LABELS[props.level])}</span>;
}

export const fmtTime = (ts: number | null | undefined) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

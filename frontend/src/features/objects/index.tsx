/**
 * Milestone 0.3 — object management dialogs: table designer, column / constraint / index /
 * view / routine / sequence / domain / exception / role / collation dialogs, PSQL editor tab,
 * privileges tab, rename, comments and drop with dependency preview.
 */
import type { ReactNode } from "react";
import { api } from "../../api";
import { errMsg, type AppApi } from "../../context";
import { registerMenu, registerObjectTab, registerTabKind } from "../../registry";
import type { MenuItem } from "../../components/ContextMenu";
import { IconPlus, IconProc, TYPE_ICONS } from "../../components/Icons";
import { typeInfo, type Selection } from "../../types";
import { ColumnDialog, ConstraintDialog, IndexDialog, TableDialog } from "./table";
import { CollationDialog, DomainDialog, ExceptionDialog, RoleDialog, SequenceDialog, ViewDialog } from "./simple";
import { FunctionDialog, PackageDialog, ProcedureDialog, PsqlEditor, TriggerDialog, openPsqlEditor } from "./routines";
import { CommentsDialog, DropDialog, RenameDialog } from "./manage";
import { PrivilegesPanel, privilegesApplies } from "./privileges";
import { IconCharset, IconCollation, IconFilterBlob, IconShadow } from "./icons";
import "./objects.css";

Object.assign(TYPE_ICONS, { collations: IconCollation, charsets: IconCharset, shadows: IconShadow, blob_filters: IconFilterBlob });

type DialogFn = (sid: string, dbid: string, name?: string) => ReactNode;
const DIALOGS: Record<string, DialogFn> = {
  tables: (s, d, n) => <TableDialog serverId={s} dbId={d} name={n} />,
  views: (s, d, n) => <ViewDialog serverId={s} dbId={d} name={n} />,
  procedures: (s, d, n) => <ProcedureDialog serverId={s} dbId={d} name={n} />,
  functions: (s, d, n) => <FunctionDialog serverId={s} dbId={d} name={n} />,
  packages: (s, d, n) => <PackageDialog serverId={s} dbId={d} name={n} />,
  triggers: (s, d, n) => <TriggerDialog serverId={s} dbId={d} name={n} />,
  sequences: (s, d, n) => <SequenceDialog serverId={s} dbId={d} name={n} />,
  domains: (s, d, n) => <DomainDialog serverId={s} dbId={d} name={n} />,
  exceptions: (s, d, n) => <ExceptionDialog serverId={s} dbId={d} name={n} />,
  indices: (s, d, n) => <IndexDialog serverId={s} dbId={d} name={n} />,
  roles: (s, d, n) => <RoleDialog serverId={s} dbId={d} name={n} />,
  collations: (s, d, n) => <CollationDialog serverId={s} dbId={d} name={n} />,
};
const PSQL = new Set(["procedures", "functions", "triggers", "packages"]);
const NO_COMMENT = new Set(["shadows", "system_tables"]);
const NO_RENAME = new Set(["charsets", "shadows", "blob_filters", "system_tables", "users"]);

function objectItems(app: AppApi, sel: Selection): MenuItem[] {
  const sid = sel.serverId, dbid = sel.dbId!, otype = sel.otype!, name = sel.name!;
  const single = typeInfo(otype)?.singular.toLowerCase() ?? otype;
  const items: MenuItem[] = [];
  if (sel.kind === "column") {
    const col = sel.column!;
    if (otype === "tables") {
      items.push(
        { label: "Edit column…", onClick: () => app.showDialog(<ColumnDialog serverId={sid} dbId={dbid} table={name} column={col} />) },
        { label: "Rename column…", onClick: () => app.showDialog(<RenameDialog serverId={sid} dbId={dbid} otype={otype} name={name} column={col} />) },
      );
    }
    if (!NO_COMMENT.has(otype)) items.push({ label: "Comment…", onClick: () => app.showDialog(<CommentsDialog serverId={sid} dbId={dbid} otype={otype} name={name} focus={col} />) });
    if (otype === "tables") items.push({ label: "Drop column…", danger: true, onClick: () => app.showDialog(<DropDialog serverId={sid} dbId={dbid} otype={otype} name={name} column={col} />) });
    return items;
  }
  const dlg = DIALOGS[otype];
  if (dlg) {
    const edit = otype === "tables" ? "Table designer…" : PSQL.has(otype) ? `${typeInfo(otype)?.singular} dialog…` : `Edit ${single}…`;
    items.push({ label: edit, onClick: () => app.showDialog(dlg(sid, dbid, name)) });
  }
  if (PSQL.has(otype)) items.push({ label: "Edit source (PSQL editor)", icon: <IconProc />, onClick: () => openPsqlEditor(app, sid, dbid, otype, name) });
  if (otype === "tables") {
    items.push(
      { label: "Add column…", icon: <IconPlus />, onClick: () => app.showDialog(<ColumnDialog serverId={sid} dbId={dbid} table={name} />) },
      { label: "Add constraint…", onClick: () => app.showDialog(<ConstraintDialog serverId={sid} dbId={dbid} table={name} />) },
      { label: "Add index…", onClick: () => app.showDialog(<IndexDialog serverId={sid} dbId={dbid} table={name} />) },
      { label: "Create trigger…", onClick: () => app.showDialog(<TriggerDialog serverId={sid} dbId={dbid} relation={name} />) },
    );
  }
  if (otype === "views") items.push({ label: "Create trigger…", onClick: () => app.showDialog(<TriggerDialog serverId={sid} dbId={dbid} relation={name} />) });
  if (otype === "indices") {
    items.push({
      label: "Recompute selectivity", onClick: async () => {
        try {
          const r = await api.execute(sid, dbid, `SET STATISTICS INDEX ${name.match(/^[A-Z][A-Z0-9_$]*$/) ? name : `"${name.replace(/"/g, '""')}"`}`);
          if (r.error) throw new Error(r.error.message);
          app.toast(`Selectivity of ${name} recomputed`);
          app.select({ ...sel });
        } catch (e) { app.toast(errMsg(e), "error"); }
      },
    });
  }
  if (!NO_COMMENT.has(otype)) items.push({ label: "Comments…", onClick: () => app.showDialog(<CommentsDialog serverId={sid} dbId={dbid} otype={otype} name={name} />) });
  if (!NO_RENAME.has(otype)) items.push({ label: "Rename…", onClick: () => app.showDialog(<RenameDialog serverId={sid} dbId={dbid} otype={otype} name={name} />) });
  return items;
}

function folderItems(app: AppApi, sel: Selection): MenuItem[] {
  const sid = sel.serverId, dbid = sel.dbId!, otype = sel.otype!;
  const dlg = DIALOGS[otype];
  const items: MenuItem[] = [];
  if (dlg) items.push({ label: `Create ${typeInfo(otype)?.singular.toLowerCase()}…`, icon: <IconPlus />, onClick: () => app.showDialog(dlg(sid, dbid)) });
  if (PSQL.has(otype)) items.push({ label: `New ${typeInfo(otype)?.singular.toLowerCase()} in PSQL editor`, onClick: () => openPsqlEditor(app, sid, dbid, otype) });
  return items;
}

registerMenu((app, sel) => {
  if (!sel.dbId || !sel.otype) return [];
  if (sel.kind === "folder") return folderItems(app, sel);
  if (sel.kind === "object" || sel.kind === "column") return objectItems(app, sel);
  return [];
});

registerObjectTab({
  id: "privileges",
  title: "Privileges",
  applies: privilegesApplies,
  render: (sel) => <PrivilegesPanel sel={sel} />,
});

registerTabKind("psql", {
  icon: <IconProc size={13} />,
  render: (tab, visible) => <PsqlEditor tab={tab} visible={visible} />,
  same: (a, b) => !!b.props?.name && a.serverId === b.serverId && a.dbId === b.dbId &&
    a.props?.otype === b.props?.otype && a.props?.name === b.props?.name,
});

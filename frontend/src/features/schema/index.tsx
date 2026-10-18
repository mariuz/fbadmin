/**
 * Schema tools (milestone 0.6): DDL extract, schema diff, ERD, import/export, copy data,
 * search objects, test data generator, unused/invalid objects report.
 */
import "./schema.css";
import type { AppApi } from "../../context";
import type { Selection } from "../../types";
import { registerMenu, registerTabKind, registerTopbar } from "../../registry";
import type { MenuItem } from "../../components/ContextMenu";
import { IconDownload, IconFilter, IconInfo, IconLog, IconPlay, IconTable, IconUpload } from "../../components/Icons";
import { ExtractTab } from "./ExtractTab";
import { DiffTab } from "./DiffTab";
import { ErdTab } from "./ErdTab";
import { ImportTab } from "./ImportTab";
import { ExportDialog } from "./ExportDialog";
import { CopyTab } from "./CopyTab";
import { SearchTab } from "./SearchTab";
import { GeneratorTab } from "./GeneratorTab";
import { ReportTab } from "./ReportTab";
import { t } from "../../i18n";

const IconErd = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="#0891b2" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="3" width="8" height="7" rx="1" /><rect x="14" y="14" width="8" height="7" rx="1" /><path d="M10 6.5h3a2 2 0 0 1 2 2V14" />
  </svg>
);
const IconDiff = (p: { size?: number }) => (
  <svg width={p.size ?? 14} height={p.size ?? 14} viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth={1.8} strokeLinecap="round">
    <path d="M6 3v12M2 9h8M14 19h8M18 3v10" />
  </svg>
);

registerTabKind("schema-extract", { icon: <IconDownload size={13} />, render: (tab) => <ExtractTab tab={tab} /> });
registerTabKind("schema-diff", { icon: <IconDiff size={13} />, render: (tab) => <DiffTab tab={tab} />, same: () => false });
registerTabKind("schema-erd", { icon: <IconErd size={13} />, render: (tab, visible) => <ErdTab tab={tab} visible={visible} /> });
registerTabKind("schema-import", { icon: <IconUpload size={13} />, render: (tab) => <ImportTab tab={tab} /> });
registerTabKind("schema-copy", { icon: <IconTable size={13} />, render: (tab) => <CopyTab tab={tab} /> });
registerTabKind("schema-search", { icon: <IconFilter size={13} />, render: (tab) => <SearchTab tab={tab} /> });
registerTabKind("schema-generate", { icon: <IconPlay size={13} />, render: (tab) => <GeneratorTab tab={tab} /> });
registerTabKind("schema-report", { icon: <IconInfo size={13} />, render: (tab) => <ReportTab tab={tab} /> });

function dbLabel(app: AppApi, sid: string, dbid: string) {
  return app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
}

export function openSchemaTool(app: AppApi, kind: string, sid: string, dbid: string, title: string, props?: Record<string, any>, name?: string) {
  app.openTab({ kind: `schema-${kind}`, title: `${dbLabel(app, sid, dbid)} · ${title}`, serverId: sid, dbId: dbid, name, props });
}

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (!sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  const open = (kind: string, title: string, props?: Record<string, any>, name?: string) => () => openSchemaTool(app, kind, sid, dbid, title, props, name);
  if (sel.kind === "database") {
    return [
      { header: t("Schema tools") },
      { label: t("Search objects…"), icon: <IconFilter />, onClick: open("search", t("search")) },
      { label: t("ERD diagram"), icon: <IconErd />, onClick: open("erd", t("ERD")) },
      { label: t("Extract DDL (full script)"), icon: <IconDownload />, onClick: open("extract", t("DDL")) },
      { label: t("Schema diff…"), icon: <IconDiff />, onClick: open("diff", t("schema diff")) },
      { label: t("Import data…"), icon: <IconUpload />, onClick: open("import", t("import")) },
      { label: t("Export data…"), icon: <IconDownload />, onClick: () => app.showDialog(<ExportDialog serverId={sid} dbId={dbid} />) },
      { label: t("Copy data to another database…"), icon: <IconTable />, onClick: open("copy", t("copy data")) },
      { label: t("Generate test data…"), icon: <IconPlay />, onClick: open("generate", t("test data")) },
      { label: t("Unused / invalid objects"), icon: <IconLog />, onClick: open("report", t("objects report")) },
    ];
  }
  if (sel.kind === "folder" && sel.otype === "tables") {
    return [
      { label: t("ERD diagram"), icon: <IconErd />, onClick: open("erd", t("ERD")) },
      { label: t("Import data…"), icon: <IconUpload />, onClick: open("import", t("import")) },
    ];
  }
  if (sel.kind === "object" && (sel.otype === "tables" || sel.otype === "views")) {
    const tbl = sel.name!;
    const items: MenuItem[] = [
      { header: t("Data") },
      { label: t("Export data…"), icon: <IconDownload />, onClick: () => app.showDialog(<ExportDialog serverId={sid} dbId={dbid} table={tbl} />) },
    ];
    if (sel.otype === "tables") {
      items.push(
        { label: t("Import data…"), icon: <IconUpload />, onClick: open("import", t("import {table}", { table: tbl }), { table: tbl }, tbl) },
        { label: t("Generate test data…"), icon: <IconPlay />, onClick: open("generate", t("test data {table}", { table: tbl }), { table: tbl }, tbl) },
        { label: t("Copy to another database…"), icon: <IconTable />, onClick: open("copy", t("copy {table}", { table: tbl }), { tables: [tbl] }, tbl) },
        { label: t("Show in ERD"), icon: <IconErd />, onClick: open("erd", t("ERD"), { focus: tbl }) },
      );
    }
    return items;
  }
  return [];
});

registerTopbar({
  id: "schema-search",
  render: (app) => {
    const sel = app.selection?.dbId ? app.selection : null;
    return (
      <button className="tb" disabled={!sel} style={{ opacity: sel ? 1 : 0.5 }} title={t("Search objects by name, comment or source")}
        onClick={() => sel && openSchemaTool(app, "search", sel.serverId, sel.dbId!, t("search"))}>
        <IconFilter /> {t("Search objects")}
      </button>
    );
  },
});

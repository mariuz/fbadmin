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

registerTabKind("schema-extract", { icon: <IconDownload size={13} />, render: (t) => <ExtractTab tab={t} /> });
registerTabKind("schema-diff", { icon: <IconDiff size={13} />, render: (t) => <DiffTab tab={t} />, same: () => false });
registerTabKind("schema-erd", { icon: <IconErd size={13} />, render: (t, visible) => <ErdTab tab={t} visible={visible} /> });
registerTabKind("schema-import", { icon: <IconUpload size={13} />, render: (t) => <ImportTab tab={t} /> });
registerTabKind("schema-copy", { icon: <IconTable size={13} />, render: (t) => <CopyTab tab={t} /> });
registerTabKind("schema-search", { icon: <IconFilter size={13} />, render: (t) => <SearchTab tab={t} /> });
registerTabKind("schema-generate", { icon: <IconPlay size={13} />, render: (t) => <GeneratorTab tab={t} /> });
registerTabKind("schema-report", { icon: <IconInfo size={13} />, render: (t) => <ReportTab tab={t} /> });

function dbLabel(app: AppApi, sid: string, dbid: string) {
  return app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? "database";
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
      { header: "Schema tools" },
      { label: "Search objects…", icon: <IconFilter />, onClick: open("search", "search") },
      { label: "ERD diagram", icon: <IconErd />, onClick: open("erd", "ERD") },
      { label: "Extract DDL (full script)", icon: <IconDownload />, onClick: open("extract", "DDL") },
      { label: "Schema diff…", icon: <IconDiff />, onClick: open("diff", "schema diff") },
      { label: "Import data…", icon: <IconUpload />, onClick: open("import", "import") },
      { label: "Export data…", icon: <IconDownload />, onClick: () => app.showDialog(<ExportDialog serverId={sid} dbId={dbid} />) },
      { label: "Copy data to another database…", icon: <IconTable />, onClick: open("copy", "copy data") },
      { label: "Generate test data…", icon: <IconPlay />, onClick: open("generate", "test data") },
      { label: "Unused / invalid objects", icon: <IconLog />, onClick: open("report", "objects report") },
    ];
  }
  if (sel.kind === "folder" && sel.otype === "tables") {
    return [
      { label: "ERD diagram", icon: <IconErd />, onClick: open("erd", "ERD") },
      { label: "Import data…", icon: <IconUpload />, onClick: open("import", "import") },
    ];
  }
  if (sel.kind === "object" && (sel.otype === "tables" || sel.otype === "views")) {
    const t = sel.name!;
    const items: MenuItem[] = [
      { header: "Data" },
      { label: "Export data…", icon: <IconDownload />, onClick: () => app.showDialog(<ExportDialog serverId={sid} dbId={dbid} table={t} />) },
    ];
    if (sel.otype === "tables") {
      items.push(
        { label: "Import data…", icon: <IconUpload />, onClick: open("import", `import ${t}`, { table: t }, t) },
        { label: "Generate test data…", icon: <IconPlay />, onClick: open("generate", `test data ${t}`, { table: t }, t) },
        { label: "Copy to another database…", icon: <IconTable />, onClick: open("copy", `copy ${t}`, { tables: [t] }, t) },
        { label: "Show in ERD", icon: <IconErd />, onClick: open("erd", "ERD", { focus: t }) },
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
      <button className="tb" disabled={!sel} style={{ opacity: sel ? 1 : 0.5 }} title="Search objects by name, comment or source"
        onClick={() => sel && openSchemaTool(app, "search", sel.serverId, sel.dbId!, "search")}>
        <IconFilter /> Search objects
      </button>
    );
  },
});

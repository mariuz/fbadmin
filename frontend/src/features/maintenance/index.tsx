/**
 * Milestone 0.5 — maintenance and server tools: backup/restore through the browser, nbackup,
 * gfix, shutdown/online, properties, gstat charts, Trace API, encryption, RDB$CONFIG, schedules.
 */
import { useEffect, useState } from "react";
import "./maintenance.css";
import { registerDashboardSection, registerMenu, registerTabKind, registerTopbar } from "../../registry";
import type { AppApi } from "../../context";
import { useApp } from "../../context";
import { N_, t } from "../../i18n";
import type { Selection } from "../../types";
import { Modal } from "../../components/Modal";
import { IconActivity, IconDownload, IconInfo, IconLog, IconUpload } from "../../components/Icons";
import { mapi, type DbState } from "./api";
import { BackupRestoreSection, RestorePanel } from "./backup";
import { GstatSection } from "./gstat";
import { ConfigSection, EncryptionSection, NbackupSection, PropertiesSection, RepairSection, ShutdownSection, StatePills } from "./tools";
import { TracePanel } from "./trace";
import { SchedulesPanel } from "./schedules";

const SECTIONS = [
  ["backup", N_("Backup & restore")],
  ["nbackup", N_("nbackup")],
  ["repair", N_("Validate & repair")],
  ["shutdown", N_("Shutdown / online")],
  ["properties", N_("Properties")],
  ["gstat", N_("Statistics (gstat)")],
  ["encryption", N_("Encryption")],
  ["config", N_("Configuration")],
  ["schedules", N_("Schedules")],
] as const;
type SectionId = (typeof SECTIONS)[number][0];

function MaintenanceTab(props: { serverId: string; dbId: string; section?: SectionId; visible: boolean }) {
  const [section, setSection] = useState<SectionId>(props.section ?? "backup");
  useEffect(() => { if (props.section) setSection(props.section); }, [props.section]);
  const app = useApp();
  const db = app.servers.find((s) => s.id === props.serverId)?.databases.find((d) => d.id === props.dbId);
  if (!db) return <div className="pad muted">{t("The database is no longer registered.")}</div>;
  const p = { serverId: props.serverId, dbId: props.dbId };
  return (
    <div className="mt">
      <div className="mt-nav">
        {SECTIONS.map(([id, label]) => (
          <button key={id} className={section === id ? "active" : ""} onClick={() => setSection(id)}>{t(label)}</button>
        ))}
        <button onClick={() => app.openTab({ kind: "trace", title: t("{name} · trace", { name: String(app.servers.find((s) => s.id === props.serverId)?.name) }), serverId: props.serverId, props: { dbId: props.dbId } })}>{t("Trace ↗")}</button>
      </div>
      {section === "schedules" ? <SchedulesPanel visible={props.visible} serverId={props.serverId} dbId={props.dbId} /> : (
        <div className="mt-body">
          <h2 style={{ marginBottom: 14 }}>{db.name} <span className="muted" style={{ fontWeight: 400, fontSize: 13 }}>{db.path}</span></h2>
          {section === "backup" && <BackupRestoreSection {...p} />}
          {section === "nbackup" && <NbackupSection {...p} />}
          {section === "repair" && <RepairSection {...p} />}
          {section === "shutdown" && <ShutdownSection {...p} />}
          {section === "properties" && <PropertiesSection {...p} />}
          {section === "gstat" && <GstatSection {...p} />}
          {section === "encryption" && <EncryptionSection {...p} />}
          {section === "config" && <ConfigSection {...p} />}
        </div>
      )}
    </div>
  );
}

function openMaintenance(app: AppApi, sid: string, dbid: string, section: SectionId) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
  app.openTab({ kind: "maintenance", title: t("{name} · maintenance", { name: db?.name ?? "db" }), serverId: sid, dbId: dbid, props: { section, nonce: Date.now() } });
}

function RestoreDialog(props: { serverId: string }) {
  const app = useApp();
  return (
    <Modal title={t("Restore on {name}", { name: String(app.servers.find((s) => s.id === props.serverId)?.name) })} wide onClose={() => app.showDialog(null)}>
      <RestorePanel serverId={props.serverId} onClose={() => app.showDialog(null)} />
    </Modal>
  );
}

registerTabKind("maintenance", {
  icon: <IconInfo size={13} />,
  render: (tab, visible) => <MaintenanceTab serverId={tab.serverId!} dbId={tab.dbId!} section={tab.props?.section} visible={visible} key={tab.id} />,
  same: (a, b) => a.serverId === b.serverId && a.dbId === b.dbId,
});
registerTabKind("trace", {
  icon: <IconLog size={13} />,
  render: (tab, visible) => <TracePanel serverId={tab.serverId!} dbId={tab.props?.dbId} visible={visible} />,
  same: (a, b) => a.serverId === b.serverId,
});
registerTabKind("schedules", {
  icon: <IconActivity size={13} />,
  render: (_tab, visible) => <SchedulesPanel visible={visible} />,
  same: () => true,
});

registerTopbar({
  id: "schedules",
  render: (app) => <button className="tb" onClick={() => app.openTab({ kind: "schedules", title: t("Schedules") })}>⏱ {t("Schedules")}</button>,
});

registerMenu((app: AppApi, sel: Selection) => {
  const srv = app.servers.find((s) => s.id === sel.serverId);
  if (!srv) return [];
  if (sel.kind === "server") {
    return [
      { header: t("Server tools") },
      { label: t("Upload & restore backup…"), icon: <IconUpload />, onClick: () => app.showDialog(<RestoreDialog serverId={srv.id} />) },
      { label: t("Trace sessions"), icon: <IconLog />, onClick: () => app.openTab({ kind: "trace", title: t("{name} · trace", { name: srv.name }), serverId: srv.id }) },
      { label: t("Scheduled jobs"), icon: <IconActivity />, onClick: () => app.openTab({ kind: "schedules", title: t("Schedules") }) },
    ];
  }
  if (sel.kind !== "database") return [];
  const dbid = sel.dbId!;
  const m = (section: SectionId) => () => openMaintenance(app, srv.id, dbid, section);
  return [
    { header: t("Maintenance tools") },
    { label: t("Backup & download / restore…"), icon: <IconDownload />, onClick: m("backup") },
    { label: t("nbackup…"), onClick: m("nbackup") },
    { label: t("Validate, sweep & limbo…"), onClick: m("repair") },
    { label: t("Shutdown / bring online…"), onClick: m("shutdown") },
    { label: t("Database properties…"), onClick: m("properties") },
    { label: t("Statistics charts (gstat)"), onClick: m("gstat") },
    { label: t("Encryption…"), onClick: m("encryption") },
    { label: t("Configuration (RDB$CONFIG)"), onClick: m("config") },
    { label: t("Schedule backups / sweeps…"), onClick: m("schedules") },
    { label: t("Trace…"), icon: <IconLog />, onClick: () => app.openTab({ kind: "trace", title: t("{name} · trace", { name: srv.name }), serverId: srv.id, props: { dbId: dbid } }) },
  ];
});

function DashboardMaintenance(props: { sel: Selection }) {
  const app = useApp();
  const [state, setState] = useState<DbState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { serverId, dbId } = props.sel;
  useEffect(() => {
    setState(null);
    setErr(null);
    mapi.properties(serverId, dbId!).then(setState).catch((e) => setErr(String(e?.message ?? e)));
  }, [serverId, dbId]);
  const go = (s: SectionId) => () => openMaintenance(app, serverId, dbId!, s);
  return (
    <>
      <h3>{t("Maintenance")}</h3>
      <div className="mt-row" style={{ marginBottom: 8 }}>
        {err ? <span className="muted">{err}</span> : state ? <StatePills state={state} /> : <span className="muted">{t("Loading state…")}</span>}
      </div>
      <div className="mt-row">
        <button className="btn" onClick={go("backup")}><IconDownload /> {t("Backup / restore")}</button>
        <button className="btn" onClick={go("gstat")}>{t("Statistics charts")}</button>
        <button className="btn" onClick={go("repair")}>{t("Validate & sweep")}</button>
        <button className="btn" onClick={go("shutdown")}>{t("Shutdown / online")}</button>
        <button className="btn" onClick={go("properties")}>{t("Properties")}</button>
        <button className="btn" onClick={go("schedules")}>{t("Schedules")}</button>
      </div>
    </>
  );
}

registerDashboardSection({
  id: "maintenance",
  render: (sel) => (sel.kind === "database" && sel.dbId ? <DashboardMaintenance sel={sel} key={sel.dbId} /> : null),
});

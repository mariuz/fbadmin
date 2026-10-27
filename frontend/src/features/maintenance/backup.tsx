import { useEffect, useRef, useState } from "react";
import { errMsg, keys, useApp } from "../../context";
import { t } from "../../i18n";
import type { Job } from "../../types";
import { IconDownload, IconRefresh, IconTrash, IconUpload } from "../../components/Icons";
import { browserDownload, fmtBytes, fmtTime, mapi, uploadRestore, type ArchiveItem } from "./api";
import {
  BackupOptionsForm, JobLog, RestoreOptionsForm, Section, backupPayload, defaultBackupOpts, defaultRestoreOpts,
  restorePayload, useBusy, useCaps,
} from "./common";

const stamp = () => new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
const dirOf = (p: string) => (p.includes("/") || p.includes("\\") ? p.replace(/[^/\\]*$/, "") : "");

export function BackupPanel(props: { serverId: string; dbId: string; onArchiveChange?: () => void }) {
  const app = useApp();
  const srv = app.servers.find((s) => s.id === props.serverId)!;
  const db = srv.databases.find((d) => d.id === props.dbId)!;
  const caps = useCaps(props.serverId);
  const [opts, setOpts] = useState(defaultBackupOpts);
  const [serverFile, setServerFile] = useState(db.path.replace(/\.(fdb|gdb)$/i, "") + `_${stamp()}.fbk`);
  const [job, setJob] = useState<Job | null>(null);
  const [encrypt, setEncrypt] = useState(false);
  const { busy, error, run } = useBusy();

  const download = () => run(async () => {
    const r = await mapi.prepareDownload(props.serverId, props.dbId, backupPayload(opts));
    browserDownload(r.url, r.filename);
    app.toast(t("Downloading {file} — the backup streams from the server", { file: r.filename }), "info");
  });
  return (
    <Section title={t("Backup")} hint={t("gbak through the Services API. “Download” streams the backup (gbak to stdout) straight to your browser — no shared filesystem with the server needed.")}>
      <BackupOptionsForm value={opts} onChange={setOpts} caps={caps} />
      <div className="mt-row" style={{ marginTop: 12 }}>
        <button className="btn primary" disabled={busy} onClick={download}><IconDownload /> {t("Download .fbk")}</button>
        <button className="btn" disabled={busy} onClick={() => run(async () => {
          setJob(await mapi.backupJob(props.serverId, props.dbId, { target: "archive", options: backupPayload(opts), encrypt }));
        })}>{t("Backup to fbadmin archive")}</button>
        <label className="check" title={t("With the backup key that encrypts (Schedules → Backup encryption)")}><input type="checkbox" checked={encrypt} onChange={(e) => setEncrypt(e.target.checked)} /> {t("encrypted")}</label>
        <span className="sep" style={{ width: 1, height: 20, background: "var(--border)" }} />
        <input className="text" style={{ flex: 1, minWidth: 220 }} value={serverFile} onChange={(e) => setServerFile(e.target.value)} title={t("Path on the Firebird server")} />
        <button className="btn" disabled={busy || !serverFile} onClick={() => run(async () => {
          setJob(await mapi.backupJob(props.serverId, props.dbId, { target: "server", backup_file: serverFile, options: backupPayload(opts) }));
        })}>{t("Backup to server file")}</button>
      </div>
      {error && <div className="err" style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{error}</div>}
      <JobLog job={job} onDone={() => props.onArchiveChange?.()} />
    </Section>
  );
}

export function RestorePanel(props: { serverId: string; defaultPath?: string; archivePath?: string | null; onClose?: () => void }) {
  const app = useApp();
  const caps = useCaps(props.serverId);
  const [source, setSource] = useState<"upload" | "archive" | "server">(props.archivePath ? "archive" : "upload");
  const [file, setFile] = useState<File | null>(null);
  const [archivePath, setArchivePath] = useState(props.archivePath ?? "");
  const [serverFile, setServerFile] = useState("");
  const [target, setTarget] = useState(props.defaultPath ?? "");
  const [registerAs, setRegisterAs] = useState("");
  const [keep, setKeep] = useState(false);
  const [opts, setOpts] = useState(defaultRestoreOpts);
  const [progress, setProgress] = useState<number | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [archive, setArchive] = useState<ArchiveItem[]>([]);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const { busy, error, run } = useBusy();
  useEffect(() => { if (props.archivePath) { setSource("archive"); setArchivePath(props.archivePath); } }, [props.archivePath]);
  useEffect(() => { mapi.archive(props.serverId).then(setArchive).catch(() => undefined); }, [props.serverId]);

  const start = () => run(async () => {
    const o = restorePayload(opts);
    if (source === "upload") {
      if (!file) throw new Error(t("Choose a .fbk file to upload"));
      await mapi.capabilities(props.serverId); // asks for the password first if needed
      setProgress(0);
      const j = await uploadRestore(props.serverId, file, { database_path: target, register_as: registerAs || null, keep, ...o as Record<string, any> }, setProgress);
      setJob(j);
    } else if (source === "archive") {
      setJob(await mapi.restoreJob(props.serverId, { source: "archive", backup_file: archivePath, database_path: target, register_as: registerAs || null, options: o }));
    } else {
      setJob(await mapi.restoreJob(props.serverId, { source: "server", backup_file: serverFile, database_path: target, register_as: registerAs || null, options: o }));
    }
  });
  return (
    <div>
      <div className="form mform">
        <label className="l">{t("Source")}</label>
        <div className="seg">
          {(["upload", "archive", "server"] as const).map((s) => (
            <button key={s} className={source === s ? "on" : ""} onClick={() => setSource(s)}>
              {s === "upload" ? t("Upload from this computer") : s === "archive" ? t("fbadmin archive") : t("File on the server")}
            </button>
          ))}
        </div>
        {source === "upload" && <>
          <span />
          <div className={"dropzone" + (over ? " over" : "")} onClick={() => input.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files[0]; if (f) setFile(f); }}>
            <IconUpload /> {file ? <b>{file.name} · {fmtBytes(file.size)}</b> : t("Drop a .fbk here or click to choose")}
            <input ref={input} type="file" accept=".fbk,.gbk,.fbz,application/octet-stream" style={{ display: "none" }}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            {progress !== null && <div className="progress"><div style={{ width: `${Math.round(progress * 100)}%` }} /></div>}
          </div>
          <span />
          <label className="check"><input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} /> {t("Keep the uploaded file in the fbadmin archive")}</label>
        </>}
        {source === "archive" && <>
          <label className="l">{t("Backup")}</label>
          <select className="text" value={archivePath} onChange={(e) => setArchivePath(e.target.value)}>
            <option value="">{t("(choose)")}</option>
            {archive.map((a) => <option key={a.path} value={a.path}>{a.path} · {fmtBytes(a.size)}</option>)}
          </select>
        </>}
        {source === "server" && <>
          <label className="l">{t("Backup file")}</label>
          <input className="text" placeholder="/path/on/server/db.fbk" value={serverFile} onChange={(e) => setServerFile(e.target.value)} />
        </>}
        <label className="l">{t("Target database")}</label>
        <input className="text" placeholder="/var/lib/firebird/data/restored.fdb" value={target} onChange={(e) => setTarget(e.target.value)} />
        <label className="l">{t("Register as")}</label>
        <input className="text" placeholder={t("optional display name")} value={registerAs} onChange={(e) => setRegisterAs(e.target.value)} />
      </div>
      <h3>{t("Restore options")}</h3>
      <RestoreOptionsForm value={opts} onChange={setOpts} caps={caps} />
      <div className="mt-row" style={{ marginTop: 12 }}>
        <button className="btn primary" disabled={busy || !target} onClick={start}><IconUpload /> {t("Start restore")}</button>
        {props.onClose && <button className="btn" onClick={props.onClose}>{t("Close")}</button>}
        {error && <span className="err" style={{ whiteSpace: "pre-wrap" }}>{error}</span>}
      </div>
      <JobLog job={job} onDone={(j) => {
        setProgress(null);
        if (j.status === "finished" && registerAs) {
          app.reloadServers();
          app.invalidate(keys.server(props.serverId));
        }
      }} />
    </div>
  );
}

export function ArchivePanel(props: { serverId: string; version: number; onRestore: (path: string) => void }) {
  const app = useApp();
  const [items, setItems] = useState<ArchiveItem[] | null>(null);
  const load = () => mapi.archive(props.serverId).then(setItems).catch((e) => app.toast(errMsg(e), "error"));
  useEffect(() => { load(); }, [props.serverId, props.version]);
  return (
    <Section title={t("fbadmin archive")} hint={t("Backups stored on the fbadmin host (manual archive backups, scheduled backups and kept uploads).")}
      right={<button className="btn small" onClick={load}><IconRefresh size={12} /> {t("Refresh")}</button>}>
      {!items ? <div className="muted">{t("Loading…")}</div> : items.length === 0 ? <div className="muted">{t("No archived backups yet.")}</div> : (
        <div className="grid-wrap">
          <table className="grid">
            <thead><tr><th>{t("Folder")}</th><th>{t("File")}</th><th>{t("Size")}</th><th>{t("Created")}</th><th /></tr></thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.path}>
                  <td>{a.folder}</td><td>{a.encrypted && <span title={t("Encrypted with key {id}", { id: a.key_id ?? "" })} aria-label={t("Encrypted with key {id}", { id: a.key_id ?? "" })}>🔒 </span>}{a.name}</td><td className="num">{fmtBytes(a.size)}</td><td>{fmtTime(a.modified)}</td>
                  <td>
                    <button className="btn small" aria-label={t("Download")} title={t("Download")} onClick={() => browserDownload(mapi.archiveUrl(props.serverId, a.path), a.name)}><IconDownload size={12} /></button>{" "}
                    {a.encrypted && <><button className="btn small" onClick={() => browserDownload(mapi.archiveUrl(props.serverId, a.path, true), a.name.replace(/\.enc$/, ""))}>{t("Download decrypted")}</button>{" "}</>}
                    <button className="btn small" onClick={() => props.onRestore(a.path)}>{t("Restore…")}</button>{" "}
                    <button className="btn small danger" onClick={async () => {
                      if (!confirm(t("Delete {path}?", { path: a.path }))) return;
                      await mapi.deleteArchive(props.serverId, a.path).catch((e) => app.toast(errMsg(e), "error"));
                      load();
                    }}><IconTrash size={12} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

export function BackupRestoreSection(props: { serverId: string; dbId: string }) {
  const app = useApp();
  const db = app.servers.find((s) => s.id === props.serverId)?.databases.find((d) => d.id === props.dbId);
  const [version, setVersion] = useState(0);
  const [restoreFrom, setRestoreFrom] = useState<string | null>(null);
  const restoreRef = useRef<HTMLDivElement>(null);
  return (
    <>
      <BackupPanel serverId={props.serverId} dbId={props.dbId} onArchiveChange={() => setVersion((v) => v + 1)} />
      <div ref={restoreRef}>
        <Section title={t("Restore")} hint={t("Upload a .fbk from your computer (streamed to gbak via stdin), restore one from the fbadmin archive, or from a file on the server.")}>
          <RestorePanel serverId={props.serverId} archivePath={restoreFrom}
            defaultPath={db ? dirOf(db.path) + (db.path.split(/[\\/]/).pop() ?? "db.fdb").replace(/\.(fdb|gdb)$/i, "") + "_restored.fdb" : ""} />
        </Section>
      </div>
      <ArchivePanel serverId={props.serverId} version={version} onRestore={(p) => {
        setRestoreFrom(p);
        restoreRef.current?.scrollIntoView({ behavior: "smooth" });
      }} />
    </>
  );
}

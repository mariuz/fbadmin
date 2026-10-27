/** Roadmap 2.2: backup encryption keys (administrators). */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { IconCopy } from "../../components/Icons";
import { fmtTime, mapi, type BackupKey } from "./api";
import { Section } from "./common";

function download(name: string, text: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/** A key shown once, to be stored somewhere else. */
function ShownKey(props: { id: string; text: string; onClose: () => void }) {
  const app = useApp();
  return (
    <div className="key-shown" role="alert">
      <p><b>{t("Recovery key {id}", { id: props.id })}</b> — {t("store it in a password manager or secret store now. Without it, encrypted backups can't be restored if fbadmin's data directory is lost.")}</p>
      <pre className="code-block mono" tabIndex={0} aria-label={t("Recovery key {id}", { id: props.id })}>{props.text}</pre>
      <div className="mt-row">
        <button className="btn small" onClick={() => { navigator.clipboard?.writeText(props.text); app.toast(t("Copied")); }}><IconCopy size={12} /> {t("Copy")}</button>
        <button className="btn small" onClick={() => download(`fbadmin-backup-key-${props.id}.txt`, props.text)}>{t("Download")}</button>
        <button className="btn small" onClick={props.onClose}>{t("I stored it")}</button>
      </div>
    </div>
  );
}

export function BackupKeysSection(props: { visible: boolean }) {
  const app = useApp();
  const [keys, setKeys] = useState<BackupKey[] | null>(null);
  const [missing, setMissing] = useState<Array<{ id: string; archived_backups: number }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<{ id: string; text: string } | null>(null);
  const [importText, setImportText] = useState("");
  const load = () => mapi.backupKeys().then((r) => { setKeys(r.keys); setMissing(r.missing); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { if (props.visible) load(); }, [props.visible]);
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); await load(); } catch (e) { app.toast(errMsg(e), "error"); } };
  return (
    <Section title={t("Backup encryption")}
      hint={t("Backup schedules (and archive backups) can be encrypted with AES-256-GCM while they stream in: the archive and the cloud copies are unreadable without the key. Keys come from FBADMIN_BACKUP_KEY or are created here and kept in fbadmin's data directory. fbadmin backup-decrypt decrypts a backup without the web server.")}>
      {error && <div className="err">{error}</div>}
      {keys && keys.length === 0 && <div className="muted">{t("No backup key yet.")}</div>}
      {keys && keys.length > 0 && <div className="grid-wrap"><table className="grid">
        <thead><tr><th>{t("Key")}</th><th>{t("From")}</th><th>{t("Created")}</th><th>{t("Archived backups")}</th><th /></tr></thead>
        <tbody>{keys.map((k) => (
          <tr key={k.id}>
            <td className="mono">{k.id}{k.active ? <span className="pill finished" style={{ marginLeft: 6 }}>{t("encrypts")}</span> : null}</td>
            <td>{k.source === "environment" ? "FBADMIN_BACKUP_KEY" : t("data directory")}</td>
            <td>{k.created ? fmtTime(k.created) : "—"}</td><td className="num">{k.archived_backups}</td>
            <td>{k.source === "stored" && <>
              {!k.active && <button className="btn small" onClick={() => act(() => mapi.activateBackupKey(k.id))}>{t("Encrypt with this key")}</button>}{" "}
              <button className="btn small" onClick={() => act(async () => { const r = await mapi.exportBackupKey(k.id); setShown({ id: r.id, text: r.key }); })}>{t("Show")}</button>{" "}
              <button className="btn small danger" onClick={() => act(async () => {
                if (!confirm(k.archived_backups
                  ? t("{n} archived backup(s) need key {id}: without it they can't be restored. Remove it anyway?", { n: k.archived_backups, id: k.id })
                  : t("Remove key {id}?", { id: k.id }))) return;
                await mapi.deleteBackupKey(k.id, k.archived_backups > 0);
              })}>{t("Remove")}</button>
            </>}</td>
          </tr>
        ))}</tbody>
      </table></div>}
      {missing.length > 0 && <div className="warn" style={{ marginTop: 6 }}>{t("Archived backups need keys fbadmin doesn't have: {ids}", { ids: missing.map((m) => `${m.id} (${m.archived_backups})`).join(", ") })}</div>}
      {shown && <ShownKey id={shown.id} text={shown.text} onClose={() => setShown(null)} />}
      <div className="mt-row" style={{ marginTop: 8 }}>
        <button className="btn small primary" onClick={() => act(async () => { const r = await mapi.createBackupKey(); setShown({ id: r.id, text: r.key }); })}>{t("Create a backup key")}</button>
        <label htmlFor="key-import" className="muted">{t("or add a key")}</label>
        <input id="key-import" className="text mono" style={{ width: 360 }} type="password" autoComplete="off" placeholder="fbk1:…" value={importText} onChange={(e) => setImportText(e.target.value)} />
        <button className="btn small" disabled={!importText.trim()} onClick={() => act(async () => { await mapi.importBackupKey(importText.trim(), false); setImportText(""); app.toast(t("Key added"), "ok"); })}>{t("Add")}</button>
      </div>
    </Section>
  );
}

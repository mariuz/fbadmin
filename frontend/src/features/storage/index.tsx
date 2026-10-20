/**
 * Milestone 1.1 — cloud backup targets: S3 (and S3-compatible services), Azure Blob Storage and
 * Google Cloud Storage. Scheduled backups upload to a target with their own retention.
 */
import { useEffect, useState } from "react";
import { registerTabKind } from "../../registry";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import { IconPlay, IconPlus, IconRefresh, IconTrash, IconUpload } from "../../components/Icons";
import { Section, useBusy } from "../maintenance/common";
import { fmtBytes, fmtTime } from "../maintenance/api";
import { sapi, type StoredObject, type StorageTarget, type TargetKind } from "./api";

const MASK = "********";

interface Field { key: string; label: string; secret?: boolean; placeholder?: string; bool?: boolean; area?: boolean; required?: boolean }

const FIELDS: Record<TargetKind, Field[]> = {
  s3: [
    { key: "bucket", label: N_("Bucket"), required: true },
    { key: "region", label: N_("Region"), placeholder: "us-east-1" },
    { key: "access_key_id", label: N_("Access key ID"), required: true },
    { key: "secret_access_key", label: N_("Secret access key"), secret: true, required: true },
    { key: "session_token", label: N_("Session token (optional)"), secret: true },
    { key: "endpoint", label: N_("Endpoint (S3-compatible services)"), placeholder: "https://<account>.r2.cloudflarestorage.com" },
    { key: "path_style", label: N_("Path-style URLs (needed by most S3-compatible services)"), bool: true },
    { key: "prefix", label: N_("Base folder (optional)"), placeholder: "backups/fbadmin" },
  ],
  azure: [
    { key: "account", label: N_("Storage account"), required: true },
    { key: "container", label: N_("Container"), required: true },
    { key: "account_key", label: N_("Account key"), secret: true },
    { key: "sas_token", label: N_("SAS token (instead of the account key)"), secret: true },
    { key: "endpoint", label: N_("Endpoint (optional)"), placeholder: "https://<account>.blob.core.windows.net" },
    { key: "prefix", label: N_("Base folder (optional)"), placeholder: "backups/fbadmin" },
  ],
  gcs: [
    { key: "bucket", label: N_("Bucket"), required: true },
    { key: "service_account_json", label: N_("Service account key (JSON)"), secret: true, area: true },
    { key: "endpoint", label: N_("Endpoint (optional)"), placeholder: "https://storage.googleapis.com" },
    { key: "prefix", label: N_("Base folder (optional)"), placeholder: "backups/fbadmin" },
  ],
};
const KIND_LABEL: Record<TargetKind, string> = { s3: N_("Amazon S3 or S3-compatible"), azure: N_("Azure Blob Storage"), gcs: N_("Google Cloud Storage") };

interface Form { id?: string; name: string; kind: TargetKind; settings: Record<string, string | boolean> }

function where(tg: StorageTarget): string {
  const s = tg.settings;
  if (tg.kind === "azure") return `${s.account ?? ""}/${s.container ?? ""}`;
  return `${s.bucket ?? ""}${s.endpoint ? ` @ ${s.endpoint}` : ""}`;
}

export function StoragePanel(props: { visible: boolean }) {
  const app = useApp();
  const [targets, setTargets] = useState<StorageTarget[]>([]);
  const [form, setForm] = useState<Form | null>(null);
  const [browse, setBrowse] = useState<{ id: string; prefix: string; items: StoredObject[] } | null>(null);
  const { busy, error, run } = useBusy();

  const load = async () => {
    try { setTargets(await sapi.targets()); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  useEffect(() => { if (props.visible) load(); }, [props.visible]);

  const save = () => run(async () => {
    if (!form) return;
    const body = { name: form.name, kind: form.kind, settings: form.settings };
    if (form.id) await sapi.update(form.id, body); else await sapi.create(body);
    setForm(null);
    await load();
  }, t("Target saved"));
  const test = (tg: StorageTarget) => run(async () => {
    const r = await sapi.test(tg.id);
    if (!r.ok) throw new Error(r.error ?? t("failed"));
    app.toast(t("{name}: write, list and delete work", { name: tg.name }));
  });
  const list = (id: string, prefix: string) => run(async () => setBrowse({ id, prefix, items: await sapi.objects(id, prefix) }));

  return (
    <div className="mt-body">
      <Section wide title={t("Backup storage")}
        hint={t("Cloud targets for scheduled backups. A schedule uploads each backup after it is made (and after its test restore, when enabled) and keeps the last N in the cloud. Secrets are never shown again; write env:NAME to read a value from an environment variable.")}
        right={<><button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>
          <button className="btn small primary" onClick={() => setForm({ name: "", kind: "s3", settings: {} })}><IconPlus size={12} /> {t("New target")}</button></>}>
        {targets.length === 0 ? <div className="muted">{t("No storage targets yet.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Kind")}</th><th>{t("Location")}</th><th /></tr></thead>
            <tbody>{targets.map((tg) => (
              <tr key={tg.id}>
                <td>{tg.name}</td><td>{t(KIND_LABEL[tg.kind])}</td><td className="mono">{where(tg)}</td>
                <td>
                  <button className="btn small" disabled={busy} onClick={() => test(tg)}><IconPlay size={12} /> {t("Test")}</button>{" "}
                  <button className="btn small" disabled={busy} onClick={() => list(tg.id, "")}><IconUpload size={12} /> {t("Browse")}</button>{" "}
                  <button className="btn small" onClick={() => setForm({ id: tg.id, name: tg.name, kind: tg.kind, settings: { ...tg.settings } })}>{t("Edit")}</button>{" "}
                  <button className="btn small danger" aria-label={t("Delete")} onClick={() => run(async () => {
                    if (!confirm(t("Delete storage target \"{name}\"? Files already uploaded stay in the cloud.", { name: tg.name }))) return;
                    await sapi.remove(tg.id);
                    await load();
                  })}><IconTrash size={12} /></button>
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        {error && <div className="err" style={{ marginTop: 8 }}>{error}</div>}
      </Section>

      {form && (
        <Section title={form.id ? t("Edit target") : t("New target")}>
          <div className="form mform">
            <label className="l" htmlFor="st-name">{t("Name")}</label>
            <input id="st-name" className="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <label className="l" htmlFor="st-kind">{t("Kind")}</label>
            <select id="st-kind" className="text" value={form.kind} disabled={!!form.id} onChange={(e) => setForm({ ...form, kind: e.target.value as TargetKind, settings: {} })}>
              {(Object.keys(KIND_LABEL) as TargetKind[]).map((k) => <option key={k} value={k}>{t(KIND_LABEL[k])}</option>)}
            </select>
            {FIELDS[form.kind].map((f) => {
              const id = `st-f-${f.key}`;
              const v = form.settings[f.key];
              const set = (val: string | boolean) => setForm({ ...form, settings: { ...form.settings, [f.key]: val } });
              // unset path_style means automatic (on with a custom endpoint), so only send it once changed
              const on = v === undefined || v === null || v === "" ? !!form.settings.endpoint : v === true || v === "true";
              if (f.bool) return [<span key={f.key + "s"} />, <label key={f.key} className="check"><input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} /> {t(f.label)}</label>];
              return [
                <label key={f.key + "l"} className="l" htmlFor={id}>{t(f.label)}{f.required ? " *" : ""}</label>,
                f.area ? <textarea key={f.key} id={id} className="text mono" rows={4} value={String(v ?? "")} placeholder={v === MASK ? "" : '{"type": "service_account", …}'} onChange={(e) => set(e.target.value)} />
                  : <input key={f.key} id={id} className="text" type={f.secret && v !== MASK ? "password" : "text"} autoComplete="off" value={String(v ?? "")} placeholder={f.placeholder} onChange={(e) => set(e.target.value)} />,
              ];
            })}
          </div>
          <div className="mt-row" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy || !form.name} onClick={save}>{t("Save")}</button>
            <button className="btn" onClick={() => setForm(null)}>{t("Cancel")}</button>
          </div>
        </Section>
      )}

      {browse && (
        <Section title={t("Files in {name}", { name: targets.find((x) => x.id === browse.id)?.name ?? "" })} right={<button className="btn small" onClick={() => setBrowse(null)}>{t("Close")}</button>}>
          <div className="mt-row" style={{ marginBottom: 8 }}>
            <input className="text mono" aria-label={t("Folder (key prefix)")} value={browse.prefix} placeholder={t("folder/")} onChange={(e) => setBrowse({ ...browse, prefix: e.target.value })} />
            <button className="btn small" disabled={busy} onClick={() => list(browse.id, browse.prefix)}>{t("List")}</button>
          </div>
          {browse.items.length === 0 ? <div className="muted">{t("No files.")}</div> : (
            <div className="grid-wrap" style={{ maxHeight: 320 }}><table className="grid">
              <thead><tr><th>{t("File")}</th><th>{t("Size")}</th><th>{t("Modified")}</th></tr></thead>
              <tbody>{browse.items.map((o) => <tr key={o.key}><td className="mono">{o.key}</td><td className="num">{fmtBytes(o.size)}</td><td>{fmtTime(o.modified)}</td></tr>)}</tbody>
            </table></div>
          )}
        </Section>
      )}
    </div>
  );
}

registerTabKind("storage", {
  icon: <IconUpload size={13} />,
  render: (_tab, visible) => <StoragePanel visible={visible} />,
  same: () => true,
});

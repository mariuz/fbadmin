import { useEffect, useRef, useState, type ReactNode } from "react";
import { errMsg, useApp } from "../../context";
import type { Job } from "../../types";
import { mapi, waitJob, type Capabilities } from "./api";

/** Fetch server capabilities once per server (cached). */
const capCache: Record<string, Promise<Capabilities>> = {};
export function useCaps(sid: string): Capabilities | null {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  useEffect(() => {
    capCache[sid] = capCache[sid] ?? mapi.capabilities(sid).catch((e) => { delete capCache[sid]; throw e; });
    capCache[sid].then(setCaps).catch(() => setCaps(null));
  }, [sid]);
  return caps;
}

export function useBusy() {
  const app = useApp();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async <T,>(fn: () => Promise<T>, okMsg?: string): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      const r = await fn();
      if (okMsg) app.toast(okMsg);
      return r;
    } catch (e) {
      setError(errMsg(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, setError, run };
}

/** Live log of a background job, polled until it finishes. */
export function JobLog(props: { job: Job | null; onDone?: (j: Job) => void; maxHeight?: number }) {
  const [job, setJob] = useState<Job | null>(props.job);
  const ref = useRef<HTMLDivElement>(null);
  const app = useApp();
  useEffect(() => {
    setJob(props.job);
    if (!props.job) return;
    app.jobStarted();
    let live = true;
    waitJob(props.job.id, (j) => live && setJob(j)).then((j) => live && props.onDone?.(j)).catch(() => undefined);
    return () => { live = false; };
  }, [props.job?.id]);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [job?.log?.length]);
  if (!job) return null;
  return (
    <div style={{ marginTop: 10 }}>
      <div className="mt-row" style={{ marginBottom: 4 }}>
        <span className={"pill " + job.status}>{job.status}</span>
        <b>{job.description}</b>
        {job.finished_at && <span className="muted">{(job.finished_at - job.started_at).toFixed(1)} s</span>}
      </div>
      {job.error && <div className="err" style={{ whiteSpace: "pre-wrap" }}>{job.error}</div>}
      <div className="log" ref={ref} style={{ maxHeight: props.maxHeight ?? 260 }}>{(job.log ?? []).join("\n") || "…"}</div>
    </div>
  );
}

export function Section(props: { title: ReactNode; hint?: ReactNode; children: ReactNode; right?: ReactNode; wide?: boolean }) {
  return (
    <div className={"mt-section" + (props.wide ? " wide" : "")}>
      <h4>{props.title}<span style={{ flex: 1 }} />{props.right}</h4>
      {props.hint && <div className="hint">{props.hint}</div>}
      {props.children}
    </div>
  );
}

export function Check(props: { label: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; note?: string }) {
  return (
    <label className={"check" + (props.disabled ? " disabled" : "")} title={props.note}>
      <input type="checkbox" checked={props.checked} disabled={props.disabled} onChange={(e) => props.onChange(e.target.checked)} />
      {props.label}{props.note && props.disabled && <span className="muted" style={{ fontSize: 11 }}>({props.note})</span>}
    </label>
  );
}

export const numOrNull = (s: string) => (s.trim() === "" ? null : Number(s));

// ------------------------------------------------------------ backup options
export interface BackupOpts {
  metadata_only: boolean; garbage_collect: boolean; transportable: boolean; ignore_checksums: boolean;
  ignore_limbo: boolean; zip: boolean; parallel_workers: string; skip_data: string; include_data: string;
  crypt_plugin: string; keyholder: string; key_name: string;
}
export const defaultBackupOpts = (): BackupOpts => ({
  metadata_only: false, garbage_collect: true, transportable: true, ignore_checksums: false, ignore_limbo: false,
  zip: false, parallel_workers: "", skip_data: "", include_data: "", crypt_plugin: "", keyholder: "", key_name: "",
});
export function backupPayload(o: BackupOpts): Record<string, unknown> {
  return {
    ...o,
    parallel_workers: numOrNull(o.parallel_workers),
    skip_data: o.skip_data || null, include_data: o.include_data || null,
    crypt_plugin: o.crypt_plugin || null, keyholder: o.keyholder || null, key_name: o.key_name || null,
  };
}

export function BackupOptionsForm(props: { value: BackupOpts; onChange: (v: BackupOpts) => void; caps: Capabilities | null }) {
  const o = props.value;
  const f = props.caps?.features ?? {};
  const set = (p: Partial<BackupOpts>) => props.onChange({ ...o, ...p });
  return (
    <>
      <div className="mt-grid" style={{ marginBottom: 10 }}>
        <Check label="Metadata only" checked={o.metadata_only} onChange={(v) => set({ metadata_only: v })} />
        <Check label="Garbage collection" checked={o.garbage_collect} onChange={(v) => set({ garbage_collect: v })} />
        <Check label="Transportable format" checked={o.transportable} onChange={(v) => set({ transportable: v })} />
        <Check label="Ignore checksums" checked={o.ignore_checksums} onChange={(v) => set({ ignore_checksums: v })} />
        <Check label="Ignore limbo transactions" checked={o.ignore_limbo} onChange={(v) => set({ ignore_limbo: v })} />
        <Check label="Compress (zip)" checked={o.zip} disabled={!f.zip} note="Firebird 4+" onChange={(v) => set({ zip: v })} />
      </div>
      <div className="form mform">
        <label className="l">Parallel workers</label>
        <input className="text" type="number" min={1} disabled={!f.parallel_workers} placeholder={f.parallel_workers ? "1" : "Firebird 5+"}
          value={o.parallel_workers} onChange={(e) => set({ parallel_workers: e.target.value })} />
        <label className="l">Skip data of tables</label>
        <input className="text" placeholder="SIMILAR TO pattern, e.g. LOG|AUDIT_%" value={o.skip_data} onChange={(e) => set({ skip_data: e.target.value })} />
        <label className="l">Include data of tables</label>
        <input className="text" disabled={!f.include_data} placeholder={f.include_data ? "SIMILAR TO pattern" : "Firebird 4+"} value={o.include_data} onChange={(e) => set({ include_data: e.target.value })} />
        <label className="l">Crypt plugin</label>
        <input className="text" disabled={!f.crypt_backup} placeholder={f.crypt_backup ? "encrypt the backup with plugin" : "Firebird 4+"} value={o.crypt_plugin} onChange={(e) => set({ crypt_plugin: e.target.value })} />
        <label className="l">Key holder / key name</label>
        <div className="mt-row">
          <input className="text" style={{ flex: 1 }} disabled={!f.crypt_backup} placeholder="key holder plugin" value={o.keyholder} onChange={(e) => set({ keyholder: e.target.value })} />
          <input className="text" style={{ flex: 1 }} disabled={!f.crypt_backup} placeholder="key name" value={o.key_name} onChange={(e) => set({ key_name: e.target.value })} />
        </div>
      </div>
    </>
  );
}

// ----------------------------------------------------------- restore options
export interface RestoreOpts {
  replace: boolean; page_size: string; buffers: string; deactivate_indexes: boolean; no_validity: boolean;
  one_at_a_time: boolean; use_all_space: boolean; metadata_only: boolean; read_only: boolean; parallel_workers: string;
  skip_data: string; include_data: string; crypt_plugin: string; keyholder: string; key_name: string; fix_fss_data: string;
}
export const defaultRestoreOpts = (): RestoreOpts => ({
  replace: false, page_size: "", buffers: "", deactivate_indexes: false, no_validity: false, one_at_a_time: false,
  use_all_space: false, metadata_only: false, read_only: false, parallel_workers: "", skip_data: "", include_data: "",
  crypt_plugin: "", keyholder: "", key_name: "", fix_fss_data: "",
});
export function restorePayload(o: RestoreOpts): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) {
    if (typeof v === "boolean") out[k] = v;
    else if (["page_size", "buffers", "parallel_workers"].includes(k)) out[k] = numOrNull(v);
    else out[k] = v || null;
  }
  return out;
}

export function RestoreOptionsForm(props: { value: RestoreOpts; onChange: (v: RestoreOpts) => void; caps: Capabilities | null }) {
  const o = props.value;
  const f = props.caps?.features ?? {};
  const set = (p: Partial<RestoreOpts>) => props.onChange({ ...o, ...p });
  return (
    <>
      <div className="mt-grid" style={{ marginBottom: 10 }}>
        <Check label="Replace existing database" checked={o.replace} onChange={(v) => set({ replace: v })} />
        <Check label="Deactivate indices" checked={o.deactivate_indexes} onChange={(v) => set({ deactivate_indexes: v })} />
        <Check label="Skip validity constraints" checked={o.no_validity} onChange={(v) => set({ no_validity: v })} />
        <Check label="Commit after each table" checked={o.one_at_a_time} onChange={(v) => set({ one_at_a_time: v })} />
        <Check label="Use all page space" checked={o.use_all_space} onChange={(v) => set({ use_all_space: v })} />
        <Check label="Metadata only" checked={o.metadata_only} onChange={(v) => set({ metadata_only: v })} />
        <Check label="Restore as read-only" checked={o.read_only} onChange={(v) => set({ read_only: v })} />
      </div>
      <div className="form mform">
        <label className="l">Page size</label>
        <select className="text" value={o.page_size} onChange={(e) => set({ page_size: e.target.value })}>
          <option value="">(keep)</option>
          {[4096, 8192, 16384, 32768].map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <label className="l">Page buffers</label>
        <input className="text" type="number" placeholder="(default)" value={o.buffers} onChange={(e) => set({ buffers: e.target.value })} />
        <label className="l">Parallel workers</label>
        <input className="text" type="number" min={1} disabled={!f.parallel_workers} placeholder={f.parallel_workers ? "1" : "Firebird 5+"}
          value={o.parallel_workers} onChange={(e) => set({ parallel_workers: e.target.value })} />
        <label className="l">Skip data of tables</label>
        <input className="text" placeholder="SIMILAR TO pattern" value={o.skip_data} onChange={(e) => set({ skip_data: e.target.value })} />
        <label className="l">Include data of tables</label>
        <input className="text" disabled={!f.include_data} placeholder={f.include_data ? "SIMILAR TO pattern" : "Firebird 4+"} value={o.include_data} onChange={(e) => set({ include_data: e.target.value })} />
        <label className="l">Crypt plugin / key</label>
        <div className="mt-row">
          <input className="text" style={{ flex: 1 }} disabled={!f.crypt_backup} placeholder={f.crypt_backup ? "crypt plugin" : "Firebird 4+"} value={o.crypt_plugin} onChange={(e) => set({ crypt_plugin: e.target.value })} />
          <input className="text" style={{ flex: 1 }} disabled={!f.crypt_backup} placeholder="key holder" value={o.keyholder} onChange={(e) => set({ keyholder: e.target.value })} />
          <input className="text" style={{ flex: 1 }} disabled={!f.crypt_backup} placeholder="key name" value={o.key_name} onChange={(e) => set({ key_name: e.target.value })} />
        </div>
        <label className="l">Fix UNICODE_FSS data</label>
        <input className="text" placeholder="charset of the stored data, e.g. WIN1252" value={o.fix_fss_data} onChange={(e) => set({ fix_fss_data: e.target.value })} />
      </div>
    </>
  );
}

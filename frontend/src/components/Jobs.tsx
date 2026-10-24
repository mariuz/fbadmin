import { useEffect, useState } from "react";
import { api } from "../api";
import { errMsg, useApp } from "../context";
import type { Job } from "../types";
import { IconRefresh } from "./Icons";
import { t } from "../i18n";
import { CancelJobButton } from "../features/maintenance/common";

export function JobsPanel(props: { visible: boolean }) {
  const app = useApp();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [open, setOpen] = useState<Job | null>(null);
  const load = async () => {
    try {
      const js = await api.jobs();
      setJobs(js);
      if (open) setOpen(await api.job(open.id));
    } catch { /* ignore */ }
  };
  useEffect(() => {
    if (!props.visible) return;
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [props.visible, open?.id]);
  const srvName = (id: string) => app.servers.find((s) => (s as any).id === id)?.name ?? id;
  return (
    <div style={{ display: "flex", height: "100%" }}>
      <div style={{ width: 460, borderRight: "1px solid var(--border)", overflow: "auto" }}>
        <div className="toolbar"><b>{t("Processes")}</b><span className="grow" /><button className="btn icon" onClick={load}><IconRefresh /></button></div>
        {jobs.length === 0 && <div className="pad muted">{t("No processes yet. Backups, restores, sweeps and statistics run here.")}</div>}
        {jobs.map((j) => (
          <div key={j.id} className="history-item" style={open?.id === j.id ? { background: "var(--select)" } : undefined}
            onClick={() => api.job(j.id).then(setOpen).catch((e) => app.toast(errMsg(e), "error"))}>
            <span className={"pill " + j.status}>{t(j.status)}</span> <b>{j.description}</b>
            <div className="muted">{srvName((j as any).server_id)} · {new Date(j.started_at * 1000).toLocaleString()}
              {j.finished_at ? ` · ${(j.finished_at - j.started_at).toFixed(1)} s` : ""}</div>
          </div>
        ))}
      </div>
      <div style={{ flex: 1, padding: 14, overflow: "auto" }}>
        {open ? (
          <>
            <h2>{open.description} <span className={"pill " + open.status}>{t(open.status)}</span> <CancelJobButton job={open} onCancel={setOpen} /></h2>
            {open.error && <p className="err">{open.error}</p>}
            <div className="log">{(open.log ?? []).join("\n") || t("(no output)")}</div>
          </>
        ) : <div className="empty-state">{t("Select a process to see its log.")}</div>}
      </div>
    </div>
  );
}

export function ServerLog(props: { serverId: string }) {
  const [log, setLog] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api.serverLog(props.serverId).then((r) => setLog(r.log)).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar"><button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button></div>
      <div className="pad" style={{ flex: 1, overflow: "auto" }}>
        {error ? <div className="err">{error}</div> : <div className="log" style={{ maxHeight: "none" }}>{log ?? t("Loading…")}</div>}
      </div>
    </div>
  );
}

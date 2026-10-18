/**
 * "About fbadmin" + opt-in update check.
 *
 * fbadmin never phones home by default. Only when the user ticks "Check for new releases"
 * (stored in this browser) does the UI call GET /api/version/latest, which makes one
 * anonymous request to the GitHub Releases API (cached for 6 hours on the server).
 * Administrators can disable it entirely with FBADMIN_UPDATE_CHECK=off.
 */
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { request } from "../../api";
import { errMsg, useApp, type AppApi } from "../../context";
import { Modal } from "../../components/Modal";
import { registerTopbar } from "../../registry";
import { t } from "../../i18n";

interface LatestInfo {
  current: string;
  enabled: boolean;
  latest: string | null;
  url: string | null;
  published_at: string | null;
  update_available: boolean;
  error: string | null;
}

const OPT_IN_KEY = "fbadmin.updateCheck";
const listeners = new Set<() => void>();

function optedIn(): boolean {
  try { return localStorage.getItem(OPT_IN_KEY) === "1"; } catch { return false; }
}
function setOptIn(v: boolean) {
  try { localStorage.setItem(OPT_IN_KEY, v ? "1" : "0"); } catch { /* storage unavailable */ }
  listeners.forEach((l) => l());
}

let cached: Promise<LatestInfo> | null = null;
function latest(refresh = false): Promise<LatestInfo> {
  if (!cached || refresh) {
    cached = request<LatestInfo>("GET", `/api/version/latest${refresh ? "?refresh=true" : ""}`);
    cached.catch(() => { cached = null; });
  }
  return cached;
}

function useLatest(): [LatestInfo | null, boolean] {
  const [on, setOn] = useState(optedIn);
  const [info, setInfo] = useState<LatestInfo | null>(null);
  useEffect(() => {
    const l = () => setOn(optedIn());
    listeners.add(l);
    return () => { listeners.delete(l); };
  }, []);
  useEffect(() => {
    if (!on) { setInfo(null); return; }
    let live = true;
    latest().then((i) => live && setInfo(i)).catch(() => { /* offline: stay quiet */ });
    return () => { live = false; };
  }, [on]);
  return [info, on];
}

/** Replaces {key} placeholders in an already translated text with React nodes. */
function rich(text: string, parts: Record<string, ReactNode>): ReactNode[] {
  return text.split(/(\{\w+\})/).map((seg, i) => {
    const m = /^\{(\w+)\}$/.exec(seg);
    return m && m[1] in parts ? <Fragment key={i}>{parts[m[1]]}</Fragment> : seg;
  });
}

interface UpdatePlan { supported: boolean; kind?: string | null; update_available?: boolean; latest?: string | null; error?: string | null; reason?: string }
interface DownloadStatus { state: string; received?: number; size?: number | null; error?: string | null }

/** Desktop app only: download the verified package for this platform and install it. */
function SelfUpdate(props: { latest: string }) {
  const app = useApp();
  const [plan, setPlan] = useState<UpdatePlan | null>(null);
  const [dl, setDl] = useState<DownloadStatus | null>(null);
  useEffect(() => { request<UpdatePlan>("GET", "/api/version/update").then(setPlan).catch(() => setPlan(null)); }, [props.latest]);
  useEffect(() => {
    if (dl?.state !== "running") return;
    const timer = setInterval(() => request<DownloadStatus>("GET", "/api/version/update/status").then(setDl).catch(() => {}), 700);
    return () => clearInterval(timer);
  }, [dl?.state]);
  if (!plan?.supported || !plan.update_available) return null;
  if (plan.error) return <p className="muted">{plan.error}</p>;
  const pct = dl?.size ? Math.round((100 * (dl.received ?? 0)) / dl.size) : null;
  const install = async () => {
    try {
      const r = await request<{ action: string }>("POST", "/api/version/update/apply");
      app.toast(r.action, "info");
    } catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <div style={{ marginTop: 8 }}>
      {!dl || dl.state === "failed" ? (
        <button className="btn small primary" onClick={() => request<DownloadStatus>("POST", "/api/version/update/download").then(setDl).catch((e) => app.toast(errMsg(e), "error"))}>
          {t("Download and install {version}", { version: props.latest })}
        </button>
      ) : dl.state === "running" ? (
        <span role="status">{t("Downloading… {percent}", { percent: pct === null ? "" : pct + " %" })}</span>
      ) : (
        <button className="btn small primary" onClick={install}>
          {plan.kind === "dmg" ? t("Open the disk image") : t("Install and restart")}
        </button>
      )}
      {dl?.state === "failed" && <div className="err">{dl.error}</div>}
      <div className="muted small">{t("The package is checked against the release's SHA256SUMS before it is installed.")}</div>
    </div>
  );
}

function AboutDialog() {
  const app = useApp();
  const [on, setOn] = useState(optedIn);
  const [version, setVersion] = useState<{ version: string; update_check: boolean } | null>(null);
  const [info, setInfo] = useState<LatestInfo | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { request<{ version: string; update_check: boolean }>("GET", "/api/version").then(setVersion).catch(() => {}); }, []);
  const check = async () => {
    setBusy(true);
    try { setInfo(await latest(true)); } catch (e) { app.toast(String(e), "error"); } finally { setBusy(false); }
  };
  const close = () => app.showDialog(null);
  return (
    <Modal title={t("About fbadmin")} onClose={close} footer={<button className="btn" onClick={close}>{t("Close")}</button>}>
      <p><b>fbadmin {version?.version ?? ""}</b> — {t("web administration for Firebird.")}{" "}
        <a href="https://github.com/mariuz/fbadmin" target="_blank" rel="noreferrer">{t("Project page")}</a></p>
      <p style={{ color: "var(--muted)" }}>{t("fbadmin sends no telemetry. The optional update check below makes one anonymous request to the GitHub Releases API and nothing else.")}</p>
      {version && !version.update_check ? (
        <p>{t("The update check is disabled by the administrator.")}</p>
      ) : (
        <>
          <label className="check">
            <input type="checkbox" checked={on} onChange={(e) => { setOn(e.target.checked); setOptIn(e.target.checked); }} />{" "}
            {t("Check GitHub for new releases")}
          </label>
          {on && (
            <div style={{ marginTop: 8 }}>
              <button className="btn small" disabled={busy} onClick={check}>{t("Check now")}</button>{" "}
              {info?.error && <span style={{ color: "var(--danger, #c00)" }}>{info.error}</span>}
              {info && !info.error && (info.update_available
                ? <><span>{rich(t("Version {version} is available: {link}"), { version: <b>{info.latest}</b>, link: <a href={info.url ?? "#"} target="_blank" rel="noreferrer">{t("release notes")}</a> })}</span>
                  <SelfUpdate latest={String(info.latest)} /></>
                : <span>{t("You have the latest version ({version}).", { version: String(info.latest) })}</span>)}
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

function UpdateBadge(props: { app: AppApi }) {
  const [info, on] = useLatest();
  const show = on && info?.update_available;
  return (
    <button className="tb" title={show ? t("fbadmin {version} is available", { version: String(info!.latest) }) : t("About fbadmin")}
      onClick={() => props.app.showDialog(<AboutDialog />)}>
      {show ? <span className="badge">{t("Update {version}", { version: String(info!.latest) })}</span> : <span style={{ fontSize: 12 }}>{t("About")}</span>}
    </button>
  );
}

registerTopbar({ id: "update-check", render: (app) => <UpdateBadge app={app} /> });

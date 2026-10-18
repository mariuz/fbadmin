/**
 * "About fbadmin" + opt-in update check.
 *
 * fbadmin never phones home by default. Only when the user ticks "Check for new releases"
 * (stored in this browser) does the UI call GET /api/version/latest, which makes one
 * anonymous request to the GitHub Releases API (cached for 6 hours on the server).
 * Administrators can disable it entirely with FBADMIN_UPDATE_CHECK=off.
 */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { useApp, type AppApi } from "../../context";
import { Modal } from "../../components/Modal";
import { registerTopbar } from "../../registry";

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
    <Modal title="About fbadmin" onClose={close} footer={<button className="btn" onClick={close}>Close</button>}>
      <p><b>fbadmin {version?.version ?? ""}</b> — web administration for Firebird.{" "}
        <a href="https://github.com/mariuz/fbadmin" target="_blank" rel="noreferrer">Project page</a></p>
      <p style={{ color: "var(--muted)" }}>fbadmin sends no telemetry. The optional update check below makes one
        anonymous request to the GitHub Releases API and nothing else.</p>
      {version && !version.update_check ? (
        <p>The update check is disabled by the administrator.</p>
      ) : (
        <>
          <label className="check">
            <input type="checkbox" checked={on} onChange={(e) => { setOn(e.target.checked); setOptIn(e.target.checked); }} />{" "}
            Check GitHub for new releases
          </label>
          {on && (
            <div style={{ marginTop: 8 }}>
              <button className="btn small" disabled={busy} onClick={check}>Check now</button>{" "}
              {info?.error && <span style={{ color: "var(--danger, #c00)" }}>{info.error}</span>}
              {info && !info.error && (info.update_available
                ? <span>Version <b>{info.latest}</b> is available: <a href={info.url ?? "#"} target="_blank" rel="noreferrer">release notes</a></span>
                : <span>You have the latest version ({info.latest}).</span>)}
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
    <button className="tb" title={show ? `fbadmin ${info!.latest} is available` : "About fbadmin"}
      onClick={() => props.app.showDialog(<AboutDialog />)}>
      {show ? <span className="badge">Update {info!.latest}</span> : <span style={{ fontSize: 12 }}>About</span>}
    </button>
  );
}

registerTopbar({ id: "update-check", render: (app) => <UpdateBadge app={app} /> });

/**
 * Server-wide "Database driver" setting: automatic (the default: the native firebird-driver on top
 * of libfbclient when a Firebird 4+ client library is available, otherwise the pure-Python
 * firebirdsql driver), native, or pure Python. Shows the driver in use and why. Administrators only
 * in server mode (the local user in desktop mode). Opened from the user/security menu. See
 * docs/DRIVERS.md.
 */
import { useEffect, useState } from "react";
import { registerTabKind } from "../../registry";
import { IconRefresh, IconServer } from "../../components/Icons";
import { N_, t } from "../../i18n";
import { registerUserMenuItem } from "../security/state";
import { Section, useBusy } from "../maintenance/common";
import { driverApi, type DriverName, type DriverStatus } from "./api";
import "../maintenance/maintenance.css";

const LABEL: Record<DriverName, string> = {
  auto: N_("Automatic (recommended): native when available, otherwise pure Python"),
  native: N_("Native (firebird-driver)"),
  firebirdsql: N_("Pure Python (firebirdsql)"),
};
const HINT: Record<DriverName, string> = {
  auto: N_("Uses the native driver when firebird-driver and a Firebird 4 or newer client library are available, and the pure Python driver otherwise or if the client library fails."),
  native: N_("firebird-driver on top of the Firebird client library (libfbclient)"),
  firebirdsql: N_("Speaks the Firebird wire protocol in Python; no client library needed."),
};
const ORDER: DriverName[] = ["auto", "native", "firebirdsql"];

function DriverPanel(props: { visible: boolean }) {
  const { busy, error, run } = useBusy();
  const [st, setSt] = useState<DriverStatus | null>(null);
  const [choice, setChoice] = useState<DriverName>("auto");
  const [lib, setLib] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const apply = (s: DriverStatus) => {
    setSt(s);
    setChoice(s.driver);
    setLib(s.fbclient ?? "");
  };
  const load = async () => apply(await driverApi.get());
  useEffect(() => { if (props.visible) run(load); }, [props.visible]);

  if (!st) return <div className="mt-body">{error ? <div className="err" role="alert">{error}</div> : <span className="muted">{t("Loading…")}</span>}</div>;

  const envDriver = st.source === "environment";
  const envLib = st.fbclient_source === "environment";
  const nat = st.native;
  const changed = choice !== st.driver || (!envLib && lib.trim() !== (st.fbclient ?? ""));
  const save = () => run(async () => {
    const body: { driver?: DriverName; fbclient?: string } = {};
    if (!envDriver) body.driver = choice;
    if (!envLib) body.fbclient = lib.trim();
    const r = await driverApi.save(body);
    apply(r);
    setNote(r.closed_connections ? t("{count} pooled connection(s) closed: new connections use the selected driver.", { count: String(r.closed_connections) }) : null);
  }, t("Driver setting saved"));

  const sourceLabel = (s: string) => (s === "environment" ? t("set by environment") : s === "settings" ? t("saved setting") : t("default"));

  return (
    <div className="mt-body driver-settings">
      <Section wide title={t("Database driver")}
        hint={t("The driver fbadmin uses for every connection to a registered server: object explorer, Query Tool, create and drop database, and the Services API (backup, restore, gstat, users, trace).")}
        right={<button className="btn small" onClick={() => run(load)} aria-label={t("Refresh")} title={t("Refresh")}><IconRefresh size={12} /></button>}>
        <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="sr-only">{t("Database driver")}</legend>
          {ORDER.map((name, i) => {
            const unavailable = name === "native" && !nat.available;
            const hintId = `fb-driver-${name}-hint`;
            return (
              <label key={name} className="check" style={{ display: "flex", alignItems: "flex-start", gap: 6, marginTop: i ? 6 : 0 }}>
                <input type="radio" name="fb-driver" value={name} checked={choice === name} disabled={envDriver || unavailable}
                  aria-describedby={hintId} onChange={() => setChoice(name)} />
                <span><b>{t(LABEL[name])}</b>
                  <span id={hintId} className="muted" style={{ display: "block" }}>
                    {t(HINT[name])}
                    {unavailable && <> {t("Not available: {reason}", { reason: nat.reason })}</>}
                  </span>
                </span>
              </label>
            );
          })}
          {envDriver && <p className="muted">{t("Set by the environment variable {name}={value}: change it there and restart fbadmin.", { name: "FBADMIN_DRIVER", value: st.env.FBADMIN_DRIVER ?? "" })}</p>}
          <div className="form-grid" style={{ marginTop: 10 }}>
            <label htmlFor="fb-driver-lib">{t("Client library (libfbclient)")}</label>
            <input id="fb-driver-lib" className="text" value={lib} disabled={envLib} placeholder={t("search the system library path")}
              onChange={(e) => setLib(e.target.value)} />
          </div>
          {envLib && <p className="muted">{t("Set by the environment variable {name}: change it there and restart fbadmin.", { name: "FBADMIN_FBCLIENT" })}</p>}
          <div style={{ marginTop: 10 }}>
            <button className="btn primary" disabled={!changed || busy} onClick={save}>{t("Save")}</button>
          </div>
        </fieldset>
        {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
        {note && <p role="status">{note}</p>}
        <p className="muted">{t("Saving a different driver closes the pooled connections, so new ones use it. Open Query Tool tabs keep their connection and driver until they are closed. Local databases always use the native driver; the event monitor always uses firebirdsql.")}</p>
      </Section>

      <Section wide title={t("Driver in use")}>
        <table className="kv driver-effective"><tbody>
          <tr><td>{t("Selected driver")}</td><td><b>{t(LABEL[st.driver])}</b> <span className="muted">({sourceLabel(st.source)})</span></td></tr>
          <tr><td>{t("Driver in use")}</td><td><b>{t(LABEL[st.effective])}</b></td></tr>
          <tr><td>{t("Reason")}</td><td>{st.reason}</td></tr>
        </tbody></table>
        {st.fallback && <p className="warn" role="status">{t("The native driver failed for a connection, so the pure Python driver was used: {reason}", { reason: st.fallback })}</p>}
      </Section>

      <Section wide title={t("Native driver availability")}>
        <table className="kv"><tbody>
          <tr><td>{t("firebird-driver package")}</td><td>{nat.package ?? <span className="muted">{t("not installed")}</span>}</td></tr>
          <tr><td>{t("Client library")}</td><td><code>{nat.library ?? st.fbclient ?? "—"}</code>{st.fbclient && <span className="muted"> ({sourceLabel(st.fbclient_source)})</span>}</td></tr>
          <tr><td>{t("Client library version")}</td><td>{nat.client_version ?? "—"}</td></tr>
          {nat.icu !== null && <tr><td>{t("Time zone names (ICU)")}</td><td>{nat.icu ? t("available") : <span className="err">{nat.icu_error}</span>}</td></tr>}
          <tr><td>{t("Status")}</td><td>{nat.available ? <span className="pill running">{t("available")}</span> : <span className="err">{nat.reason}</span>}</td></tr>
        </tbody></table>
        {nat.embedded_library && <p className="muted">{t("The embedded engine's client library is in use: one process can load only one libfbclient, so it serves remote servers too.")}</p>}
        {nat.restart_required && <p className="warn" role="status">{t("A different client library is already loaded in this process: restart fbadmin to use the configured one.")}</p>}
        {nat.available && nat.client_major !== null && nat.client_major < 4 && (
          <p className="warn">{t("This is a Firebird {version} client: INT128, DECFLOAT and time zone types of Firebird 4+ servers need a Firebird 4 or newer client library.", { version: String(nat.client_major) })}</p>
        )}
      </Section>
    </div>
  );
}

registerTabKind("driver-settings", {
  icon: <IconServer size={13} />,
  render: (_tab, visible) => <DriverPanel visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});

registerUserMenuItem({
  id: "driver-settings",
  admin: true,
  label: () => t("Database driver…"),
  show: () => true,
  run: (app) => app.openTab({ kind: "driver-settings", title: t("Database driver") }),
});

/**
 * Roadmap 2.2 — Kubernetes operator integration: the Firebird clusters of the cloudnative-firebird
 * operator, their instances, backups, databases and events, and the operator's day-2 actions (backup,
 * restore, scale, hibernate, rolling restart, switchover, fencing), plus registering a cluster as an
 * fbadmin server. fbadmin edits the resources like the operator's kubectl plugin; the operator does the work.
 */
import { useEffect, useState } from "react";
import "./kubernetes.css";
import { registerTabKind, registerTopbar } from "../../registry";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import { IconRefresh } from "../../components/Icons";
import { isAdmin, useAuth } from "../security/state";
import { Section } from "../maintenance/common";
import { fmtTime } from "../maintenance/api";
import { kapi, type ClusterDetail, type ClusterSummary, type KubeStatus } from "./api";

const PHASE_CLASS: Record<string, string> = {
  Running: "ok", Completed: "ok", Ready: "ok", Applied: "ok",
  Creating: "busy", Updating: "busy", Upgrading: "busy", Pending: "busy", Restoring: "busy", Applying: "busy",
  Degraded: "bad", Failed: "bad", Deleting: "busy", Dropping: "busy", Paused: "idle", Hibernated: "idle",
};
// phases come from the operator; listed so they can be translated
N_("Running"); N_("Completed"); N_("Ready"); N_("Applied"); N_("Creating"); N_("Updating"); N_("Upgrading"); N_("Pending");
N_("Restoring"); N_("Applying"); N_("Degraded"); N_("Failed"); N_("Deleting"); N_("Dropping"); N_("Paused"); N_("Hibernated");
N_("primary"); N_("replica");

function Phase(props: { phase: string }) {
  return <span className={"k8s-phase " + (PHASE_CLASS[props.phase] ?? "idle")}>{t(props.phase)}</span>;
}

let statusCache: Promise<KubeStatus> | null = null;
function loadStatus(force = false) {
  if (!statusCache || force) statusCache = kapi.status().catch((e) => ({ enabled: false, reason: errMsg(e) }));
  return statusCache;
}

function TopbarButton() {
  const app = useApp();
  const auth = useAuth();
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (!auth || !isAdmin(auth)) { setOn(false); return; }
    loadStatus().then((s) => setOn(s.enabled));
  }, [auth]);
  if (!on) return null;
  return <button className="tb" onClick={() => app.openTab({ kind: "kubernetes", title: t("Kubernetes") })}>⎈ {t("Kubernetes")}</button>;
}

function age(ts: number | null) {
  return ts ? fmtTime(ts) : "—";
}

function useBusy(onDone: () => void) {
  const app = useApp();
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<{ message: string } | unknown>, confirmText?: string) => {
    if (confirmText && !confirm(confirmText)) return;
    setBusy(true);
    try {
      const r = await fn();
      const msg = (r as { message?: string })?.message;
      if (msg) app.toast(msg, "ok");
      onDone();
    } catch (e) {
      app.toast(errMsg(e), "error");
    } finally {
      setBusy(false);
    }
  };
  return { busy, run };
}

function RegisterForm(props: { c: ClusterDetail; onDone: () => void }) {
  const app = useApp();
  const auth = useAuth();
  const [host, setHost] = useState("");
  const [port, setPort] = useState("3050");
  const [password, setPassword] = useState("");
  const [shared, setShared] = useState(false);
  const { busy, run } = useBusy(props.onDone);
  const c = props.c;
  return (
    <div className="form mform k8s-form">
      <label className="l" htmlFor="k8s-host">{t("Host")}</label>
      <input id="k8s-host" className="text mono" value={host} placeholder={`${c.name}.${c.namespace}.svc`} onChange={(e) => setHost(e.target.value)} />
      <label className="l" htmlFor="k8s-port">{t("Port")}</label>
      <input id="k8s-port" className="text" type="number" value={port} onChange={(e) => setPort(e.target.value)} />
      <label className="l" htmlFor="k8s-pw">{t("SYSDBA password")}</label>
      <input id="k8s-pw" className="text" type="password" autoComplete="new-password" value={password}
        placeholder={t("default: from the Secret {name}", { name: c.superuser_secret ?? "" })} onChange={(e) => setPassword(e.target.value)} />
      {auth?.server_mode && <><span /><label className="check"><input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} /> {t("Shared with every fbadmin user")}</label></>}
      <span />
      <div className="mt-row">
        <button className="btn primary" disabled={busy} onClick={() => run(async () => {
          const r = await kapi.register(c.namespace, c.name, { host: host.trim() || null, port: Number(port) || 3050, password: password || null, shared });
          await app.reloadServers();
          return { message: r.created ? t("Registered as an fbadmin server with {n} database(s)", { n: r.databases.length }) : t("Server registration updated") };
        })}>{c.registered_server_id ? t("Update registration") : t("Register in fbadmin")}</button>
      </div>
    </div>
  );
}

function ClusterView(props: { ns: string; name: string; refreshKey: number }) {
  const app = useApp();
  const [c, setC] = useState<ClusterDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [instances, setInstances] = useState("");
  const [restoreFrom, setRestoreFrom] = useState("");
  const [restoreTarget, setRestoreTarget] = useState("");
  const [backupType, setBackupType] = useState<"logical" | "physical">("logical");
  const [logs, setLogs] = useState<{ pod: string; text: string } | null>(null);
  const [showRegister, setShowRegister] = useState(false);
  const load = () => kapi.cluster(props.ns, props.name).then((d) => { setC(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, [props.ns, props.name, props.refreshKey]);
  const { busy, run } = useBusy(load);
  if (!c) return <div className="muted">{error ?? t("Loading…")}</div>;
  const { namespace: ns, name } = c;
  const completed = c.backups.filter((b) => b.phase === "Completed" && b.type !== "volumeSnapshot");
  return (
    <>
      <Section wide title={<>{t("Cluster {name}", { name })} <Phase phase={c.phase} /></>}>
        {error && <div className="err">{error}</div>}
        <dl className="k8s-facts">
          <dt>{t("Namespace")}</dt><dd>{ns}</dd>
          <dt>{t("Instances")}</dt><dd>{t("{ready} of {n} ready", { ready: c.ready, n: c.instances })}</dd>
          <dt>{t("Primary")}</dt><dd className="mono">{c.primary ?? "—"}{c.switchover_to ? ` → ${c.switchover_to}` : ""}</dd>
          <dt>{t("Image")}</dt><dd className="mono">{c.image ?? "—"}</dd>
          <dt>{t("Replication")}</dt><dd>{c.replication ? (c.replication_mode ?? t("on")) : t("off")}</dd>
          <dt>{t("Database")}</dt><dd className="mono">{c.database ?? "—"}</dd>
          {c.phase_reason && <><dt>{t("Reason")}</dt><dd>{c.phase_reason}</dd></>}
          {c.fenced.length > 0 && <><dt>{t("Fenced")}</dt><dd className="mono">{c.fenced.join(", ")}</dd></>}
        </dl>
        <div className="k8s-actions">
          <span className="mt-row">
            <select className="text" aria-label={t("Backup type")} value={backupType} onChange={(e) => setBackupType(e.target.value as "logical" | "physical")}>
              <option value="logical">{t("logical (gbak)")}</option><option value="physical">{t("physical (nbackup level 0)")}</option>
            </select>
            <button className="btn small" disabled={busy || c.hibernated} onClick={() => run(() => kapi.backup(ns, name, { type: backupType, level: backupType === "physical" ? 0 : null }))}>{t("Back up now")}</button>
          </span>
          <span className="mt-row">
            <input className="text" type="number" min={1} max={32} style={{ width: 70 }} aria-label={t("Instances")} value={instances} placeholder={String(c.instances)} onChange={(e) => setInstances(e.target.value)} />
            <button className="btn small" disabled={busy || !instances || Number(instances) === c.instances}
              onClick={() => run(() => kapi.scale(ns, name, Number(instances)), t("Change cluster {name} to {n} instance(s)?", { name, n: instances }))}>{t("Scale")}</button>
          </span>
          <button className="btn small" disabled={busy} onClick={() => run(() => kapi.hibernate(ns, name, !c.hibernated),
            c.hibernated ? undefined : t("Hibernate cluster {name}? Its instances stop; the volumes are kept.", { name }))}>{c.hibernated ? t("Resume") : t("Hibernate")}</button>
          <button className="btn small" disabled={busy || c.hibernated} onClick={() => run(() => kapi.restart(ns, name),
            t("Restart every instance of {name}, the primary last?", { name }))}>{t("Rolling restart")}</button>
          {c.registered_server_id
            ? <button className="btn small" onClick={() => app.select({ kind: "server", serverId: c.registered_server_id! })}>{t("Show in the Object Explorer")}</button>
            : null}
          <button className="btn small" aria-expanded={showRegister} onClick={() => setShowRegister(!showRegister)}>{c.registered_server_id ? t("Registration…") : t("Register in fbadmin…")}</button>
        </div>
        {showRegister && <RegisterForm c={c} onDone={() => { setShowRegister(false); load(); }} />}
      </Section>

      <Section wide title={t("Instances")}>
        <div className="grid-wrap"><table className="grid">
          <thead><tr><th>{t("Pod")}</th><th>{t("Role")}</th><th>{t("Ready")}</th><th>{t("Node")}</th><th>{t("Restarts")}</th><th>{t("Started")}</th><th /></tr></thead>
          <tbody>{c.pods.map((p) => (
            <tr key={p.name}>
              <td className="mono">{p.name}{p.fenced ? ` (${t("fenced")})` : ""}</td><td>{t(p.role)}</td>
              <td>{p.ready ? "✓" : <span className="err">✕</span>}</td><td className="mono">{p.node ?? "—"}</td>
              <td className="num">{p.restarts}</td><td>{age(p.started)}</td>
              <td className="k8s-rowbtns">
                <button className="btn small" onClick={() => kapi.logs(ns, name, p.name).then(setLogs).catch((e) => app.toast(errMsg(e), "error"))}>{t("Logs")}</button>
                <button className="btn small" disabled={busy} onClick={() => run(() => kapi.restart(ns, name, p.name), t("Restart instance {pod}?", { pod: p.name }))}>{t("Restart")}</button>
                {c.replication && p.role !== "primary" && <button className="btn small" disabled={busy}
                  onClick={() => run(() => kapi.switchover(ns, name, p.name), t("Make {pod} the primary (planned switchover)?", { pod: p.name }))}>{t("Promote")}</button>}
                <button className="btn small" disabled={busy} onClick={() => run(() => kapi.fence(ns, name, !p.fenced, p.name),
                  p.fenced ? undefined : t("Fence {pod}? Its database is shut down; the pod keeps running.", { pod: p.name }))}>{p.fenced ? t("Unfence") : t("Fence")}</button>
              </td>
            </tr>
          ))}</tbody>
        </table></div>
        {logs && <div className="k8s-logs">
          <div className="mt-row"><b className="mono">{logs.pod}</b><span style={{ flex: 1 }} /><button className="btn small" onClick={() => setLogs(null)}>{t("Close")}</button></div>
          <pre aria-label={t("Logs of {pod}", { pod: logs.pod })} tabIndex={0}>{logs.text || t("(empty)")}</pre>
        </div>}
      </Section>

      <Section wide title={t("Backups")}>
        {c.backups.length === 0 ? <div className="muted">{t("No backups yet.")}</div> : (
          <div className="grid-wrap" style={{ maxHeight: 280 }}><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Type")}</th><th>{t("Status")}</th><th>{t("Started")}</th><th>{t("Duration")}</th><th>{t("Location")}</th></tr></thead>
            <tbody>{c.backups.map((b) => (
              <tr key={b.name}>
                <td className="mono">{b.name}</td><td>{b.type}{b.level != null ? ` ${b.level}` : ""}{b.database ? ` · ${b.database}` : ""}</td>
                <td><Phase phase={b.phase} />{b.verified ? ` ${t("verified")}` : ""}</td><td>{age(b.started)}</td>
                <td className="num">{b.started && b.completed ? `${Math.round(b.completed - b.started)} s` : "—"}</td>
                <td style={{ whiteSpace: "normal" }}>{b.error ? <span className="err">{b.error}</span> : <span className="mono">{b.location ?? ""}</span>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        {completed.length > 0 && <div className="mt-row k8s-restore">
          <label htmlFor="k8s-rfrom">{t("Restore")}</label>
          <select id="k8s-rfrom" className="text" value={restoreFrom} onChange={(e) => setRestoreFrom(e.target.value)}>
            <option value="">{t("choose a completed backup…")}</option>
            {completed.map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
          </select>
          <input className="text mono" aria-label={t("Into the file")} placeholder={t("into the file (default restore-<name>.fdb)")} value={restoreTarget} onChange={(e) => setRestoreTarget(e.target.value)} />
          <button className="btn small" disabled={busy || !restoreFrom} onClick={() => run(() => kapi.restore(ns, name, restoreFrom, restoreTarget.trim() || undefined),
            t("Restore {backup} into a new file on the primary? The cluster database is not replaced.", { backup: restoreFrom }))}>{t("Restore")}</button>
        </div>}
        {c.scheduled_backups.length > 0 && <>
          <h3>{t("Scheduled backups")}</h3>
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Name")}</th><th>{t("Schedule")}</th><th>{t("Type")}</th><th>{t("Last successful")}</th><th>{t("Last backup")}</th></tr></thead>
            <tbody>{c.scheduled_backups.map((s) => (
              <tr key={s.name}><td className="mono">{s.suspended ? "⏸ " : ""}{s.name}</td><td className="mono">{s.schedule}</td><td>{s.type}</td>
                <td>{age(s.last_successful)}</td><td className="mono">{s.last_backup ?? "—"}</td></tr>
            ))}</tbody>
          </table></div>
        </>}
      </Section>

      {(c.databases.length > 0 || c.users.length > 0) && <Section wide title={t("Databases and users")}>
        {c.databases.length > 0 && <div className="grid-wrap"><table className="grid">
          <thead><tr><th>{t("FirebirdDatabase")}</th><th>{t("File")}</th><th>{t("Status")}</th></tr></thead>
          <tbody>{c.databases.map((d) => <tr key={d.name}><td className="mono">{d.name}</td><td className="mono">{d.path ?? d.file ?? "—"}</td><td><Phase phase={d.phase} /></td></tr>)}</tbody>
        </table></div>}
        {c.users.length > 0 && <div className="grid-wrap"><table className="grid">
          <thead><tr><th>{t("FirebirdUser")}</th><th>{t("User")}</th><th>{t("Admin")}</th><th>{t("Status")}</th></tr></thead>
          <tbody>{c.users.map((u) => <tr key={u.name}><td className="mono">{u.name}</td><td className="mono">{u.username ?? "—"}</td><td>{u.admin ? "✓" : ""}</td><td><Phase phase={u.phase} /></td></tr>)}</tbody>
        </table></div>}
      </Section>}

      <Section wide title={t("Events and conditions")}>
        {c.conditions.length > 0 && <div className="k8s-conds">{c.conditions.map((x) => (
          <span key={x.type} className={"k8s-cond " + (x.status === "True" ? (x.type === "Degraded" ? "bad" : "ok") : "idle")} title={x.message ?? ""}>{x.type}: {x.status}</span>
        ))}</div>}
        {c.events.length === 0 ? <div className="muted">{t("No recent events.")}</div> : (
          <div className="grid-wrap" style={{ maxHeight: 240 }}><table className="grid">
            <thead><tr><th>{t("Time")}</th><th>{t("Type")}</th><th>{t("Reason")}</th><th>{t("Message")}</th></tr></thead>
            <tbody>{c.events.map((e, i) => <tr key={i}><td>{age(e.time)}</td><td>{e.type}</td><td>{e.reason}</td><td style={{ whiteSpace: "normal" }}>{e.message}{e.count && e.count > 1 ? ` (×${e.count})` : ""}</td></tr>)}</tbody>
          </table></div>
        )}
      </Section>
    </>
  );
}

function KubernetesPanel(props: { visible: boolean }) {
  const [status, setStatus] = useState<KubeStatus | null>(null);
  const [items, setItems] = useState<ClusterSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<{ ns: string; name: string } | null>(null);
  const [tick, setTick] = useState(0);
  const load = async () => {
    try {
      const s = await loadStatus(true);
      setStatus(s);
      if (s.enabled && s.operator) {
        const list = await kapi.clusters();
        setItems(list);
        setSel((cur) => cur ?? (list[0] ? { ns: list[0].namespace, name: list[0].name } : null));
      }
      setError(null);
    } catch (e) { setError(errMsg(e)); }
    setTick((x) => x + 1);
  };
  useEffect(() => {
    if (!props.visible) return;
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [props.visible]);
  return (
    <div className="mt-body k8s">
      <Section wide title={t("Firebird clusters (cloudnative-firebird)")}
        hint={status?.enabled ? t("{source}, API server {server}; namespaces: {ns}", { source: status.source ?? "", server: status.server ?? "", ns: status.namespaces ? status.namespaces.join(", ") : t("all") }) : undefined}
        right={<button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>}>
        {status && !status.enabled && <div className="muted">{status.reason}</div>}
        {status?.enabled && status.operator === false && <div className="err">{status.error}</div>}
        {error && <div className="err">{error}</div>}
        {status?.operator && (items.length === 0 ? <div className="muted">{t("No FirebirdCluster resources in these namespaces.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Namespace")}</th><th>{t("Name")}</th><th>{t("Phase")}</th><th>{t("Ready")}</th><th>{t("Primary")}</th><th>{t("Image")}</th><th>{t("fbadmin server")}</th></tr></thead>
            <tbody>{items.map((c) => {
              const on = sel?.ns === c.namespace && sel?.name === c.name;
              return (
                <tr key={c.namespace + "/" + c.name} className={on ? "sel" : ""} style={{ cursor: "pointer" }} aria-selected={on}
                  tabIndex={0} onClick={() => setSel({ ns: c.namespace, name: c.name })} onKeyDown={(e) => e.key === "Enter" && setSel({ ns: c.namespace, name: c.name })}>
                  <td>{c.namespace}</td><td className="mono">{c.name}</td><td><Phase phase={c.phase} />{c.hibernated ? " ⏾" : ""}</td>
                  <td className="num">{c.ready}/{c.instances}</td><td className="mono">{c.primary ?? "—"}</td><td className="mono">{c.image ?? "—"}</td>
                  <td>{c.registered_server_id ? "✓" : "—"}</td>
                </tr>
              );
            })}</tbody>
          </table></div>
        ))}
      </Section>
      {sel && status?.operator && <ClusterView ns={sel.ns} name={sel.name} refreshKey={tick} />}
    </div>
  );
}

registerTabKind("kubernetes", {
  render: (_tab, visible) => <KubernetesPanel visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});

registerTopbar({ id: "kubernetes", render: () => <TopbarButton /> });

/** Roadmap 2.2: replication setup wizard - primary and replica end to end, then a check that changes arrive. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import { t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconCopy } from "../../components/Icons";
import type { Job } from "../../types";
import { JobLog, Section } from "../maintenance/common";
import "../maintenance/maintenance.css";
import "../monitoring/monitoring.css";
import "./setup.css";
import { sapi, type CheckState, type Plan, type RunResult, type SetupBody } from "./api";

interface Form {
  server: string; path: string; name: string; mode: "async" | "sync"; transfer: "shared" | "manual";
  all: boolean; tables: string[]; journal: string; archive: string; source: string; timeout: string;
  copyDir: string; copyDirReplica: string; syncAddress: string; syncUser: string; syncPassword: string;
}

const empty = (): Form => ({
  server: "", path: "", name: "", mode: "async", transfer: "shared", all: true, tables: [], journal: "", archive: "",
  source: "", timeout: "10", copyDir: "", copyDirReplica: "", syncAddress: "", syncUser: "", syncPassword: "",
});

function replicaDefault(path: string): string {
  const m = /^(.*?)([^\\/]+?)(\.[^.\\/]+)?$/.exec(path);
  return m ? `${m[1]}${m[2]}_replica${m[3] ?? ".fdb"}` : path + "_replica";
}

function body(f: Form): SetupBody {
  const v = (s: string) => (s.trim() ? s.trim() : null);
  return {
    replica_server_id: f.server, replica_path: f.path.trim(), replica_name: v(f.name), mode: f.mode, transfer: f.transfer,
    tables: f.all ? null : f.tables, journal_directory: v(f.journal), archive_directory: v(f.archive), source_directory: v(f.source),
    archive_timeout: f.timeout.trim() ? Number(f.timeout) : null, copy_directory: v(f.copyDir), copy_directory_replica: v(f.copyDirReplica),
    sync_address: v(f.syncAddress), sync_user: v(f.syncUser), sync_password: f.syncPassword || null,
  };
}

function download(name: string, text: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function Field(props: { id: string; label: string; children: ReactNode }) {
  return <><label className="l" htmlFor={props.id}>{props.label}</label>{props.children}</>;
}

/** A replication.conf block: copy, download, or write it into a file fbadmin can reach. */
function ConfBlock(props: { id: string; database: string; block: string; onWritten: (v: boolean) => void; written: boolean }) {
  const app = useApp();
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="rs-conf">
      <div className="mt-row">
        <button className="btn small" onClick={() => { navigator.clipboard?.writeText(props.block); app.toast(t("Copied")); }}><IconCopy size={12} /> {t("Copy")}</button>
        <button className="btn small" onClick={() => download("replication.conf.block", props.block)}>{t("Download")}</button>
      </div>
      <pre className="code-block" tabIndex={0} aria-label={t("replication.conf block for {db}", { db: props.database })}>{props.block}</pre>
      <div className="mt-row">
        <label htmlFor={props.id + "-path"} className="muted">{t("Or write it into")}</label>
        <input id={props.id + "-path"} className="text mono rs-path" placeholder="/opt/firebird/replication.conf" value={path} onChange={(e) => setPath(e.target.value)} />
        <button className="btn small" disabled={!path.trim() || busy} onClick={async () => {
          setBusy(true);
          try {
            const r = await sapi.writeConf(path.trim(), props.database, props.block);
            const v = { path: r.path, backup: r.backup };
            app.toast(r.replaced ? t("Block replaced in {path} (copy: {backup})", v) : t("Block added to {path} (copy: {backup})", v), "ok");
            props.onWritten(true);
          } catch (e) { app.toast(errMsg(e), "error"); } finally { setBusy(false); }
        }}>{t("Write")}</button>
      </div>
      <p className="muted">{t("fbadmin writes only an existing file named replication.conf on its own machine or a mounted volume (in server mode only the files listed in FBADMIN_REPLICATION_CONF), and keeps a copy of the old file next to it.")}</p>
      <label className="check"><input type="checkbox" checked={props.written} onChange={(e) => props.onWritten(e.target.checked)} /> {t("The block is in replication.conf")}</label>
    </div>
  );
}

const LEVEL: Record<string, string> = { error: "critical", warning: "warning", info: "info" };

function SetupTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId, dbId } = props;
  const db = app.servers.find((s) => s.id === serverId)?.databases.find((d) => d.id === dbId);
  const [st, setSt] = useState<Awaited<ReturnType<typeof sapi.status>> | null>(null);
  const [f, setF] = useState<Form>(empty);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [primaryWritten, setPrimaryWritten] = useState(false);
  const [replicaWritten, setReplicaWritten] = useState(false);
  const [restarted, setRestarted] = useState(false);
  const [others, setOthers] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [copyOnReplica, setCopyOnReplica] = useState("");
  const [check, setCheck] = useState<CheckState | null>(null);
  const [checking, setChecking] = useState(false);
  const live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);

  useEffect(() => {
    sapi.status(serverId, dbId).then((s) => {
      setSt(s);
      setF((x) => ({ ...x, path: x.path || replicaDefault(s.database_name || db?.path || ""), name: x.name || `${db?.name ?? ""} (replica)` }));
    }).catch((e) => setError(errMsg(e)));
  }, [serverId, dbId]);

  const servers = app.servers.filter((s) => !s.embedded && !s.read_only);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => { setF((x) => ({ ...x, [k]: v })); if (plan) setStale(true); };
  const resolved = plan?.settings;
  const replicaDb = result?.replica_db_id;

  const doPlan = async () => {
    setBusy(true); setError(null);
    try { setPlan(await sapi.plan(serverId, dbId, body(f))); setStale(false); }
    catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  };
  const onDone = async (j: Job) => {
    try {
      const r = await sapi.result(serverId, dbId, j.id);
      setResult(r);
      if (r.replica_db_id) await app.reloadServers();
    } catch (e) { setError(errMsg(e)); }
  };
  const sendCheck = async () => {
    if (!result?.replica_db_id) return;
    setChecking(true); setCheck(null);
    try {
      const sent = await sapi.check(serverId, dbId, result.replica_server_id, result.replica_db_id);
      const end = Date.now() + 180_000;
      while (live.current && Date.now() < end) {
        const c = await sapi.poll(serverId, dbId, sent.token).catch(() => null);
        if (c) setCheck(c);
        if (c?.arrived) break;
        await new Promise((r) => setTimeout(r, 1500));
      }
    } catch (e) { app.toast(errMsg(e), "error"); } finally { setChecking(false); }
  };

  if (st && !st.supported) {
    return <div className="mt-body"><div className="err">{t("Built-in replication needs Firebird 4.0 or newer; this server runs {version}.", { version: st.engine_version })}</div></div>;
  }
  const errors = plan?.checks.filter((c) => c.level === "error").length ?? 0;
  const canRun = !!plan && plan.ok && !stale && primaryWritten && !job;
  const async = f.mode === "async";

  return (
    <div className="mt-body rs">
      <Section wide title={t("1. The replica")} hint={t("fbadmin makes the replica from a physical copy of this database (nbackup), so it keeps the record formats and the replication sequence and applies exactly the changes made after the copy.")}>
        <div className="form mform">
          <Field id="rs-server" label={t("Replica server")}>
            <select id="rs-server" className="text" value={f.server} onChange={(e) => set("server", e.target.value)}>
              <option value="">{t("choose a server…")}</option>
              {servers.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.host}:{s.port})</option>)}
            </select>
          </Field>
          <Field id="rs-path" label={t("Replica file (on that server)")}>
            <input id="rs-path" className="text mono" value={f.path} onChange={(e) => set("path", e.target.value)} />
          </Field>
          <Field id="rs-name" label={t("Name in fbadmin")}>
            <input id="rs-name" className="text" value={f.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <span className="l">{t("Mode")}</span>
          <div className="rs-radios" role="radiogroup" aria-label={t("Mode")}>
            <label className="check"><input type="radio" name="rs-mode" checked={async} onChange={() => set("mode", "async")} /> {t("Asynchronous: journal segments archived for the replica")}</label>
            <label className="check"><input type="radio" name="rs-mode" checked={!async} onChange={() => set("mode", "sync")} /> {t("Synchronous: each commit is sent to the replica")}</label>
          </div>
          <span className="l">{t("Tables")}</span>
          <div>
            <label className="check"><input type="checkbox" checked={f.all} onChange={(e) => set("all", e.target.checked)} /> {t("All tables, also tables created later")}</label>
            {!f.all && <div className="rs-tables">{(st?.tables ?? []).filter((x) => x.name !== "FBADMIN_REPLICATION_CHECK").map((x) => (
              <label key={x.name} className="check"><input type="checkbox" checked={f.tables.includes(x.name)}
                onChange={(e) => set("tables", e.target.checked ? [...f.tables, x.name] : f.tables.filter((n) => n !== x.name))} /> <span className="mono">{x.name}</span>{!x.has_key && <span className="warn"> ▲</span>}</label>
            ))}</div>}
          </div>
          {async ? <>
            <Field id="rs-journal" label={t("Journal directory")}>
              <input id="rs-journal" className="text mono" placeholder={resolved?.journal_directory ?? t("default: next to the database")} value={f.journal} onChange={(e) => set("journal", e.target.value)} />
            </Field>
            <Field id="rs-archive" label={t("Archive directory")}>
              <input id="rs-archive" className="text mono" placeholder={resolved?.archive_directory ?? t("default: next to the database")} value={f.archive} onChange={(e) => set("archive", e.target.value)} />
            </Field>
            <Field id="rs-source" label={t("The archive, as the replica server sees it")}>
              <input id="rs-source" className="text mono" placeholder={resolved?.source_directory ?? t("default: the same path")} value={f.source} onChange={(e) => set("source", e.target.value)} />
            </Field>
            <Field id="rs-timeout" label={t("Archive after (seconds)")}>
              <input id="rs-timeout" className="text" type="number" min={1} max={3600} style={{ width: 90 }} value={f.timeout} onChange={(e) => set("timeout", e.target.value)} />
            </Field>
          </> : <>
            <Field id="rs-addr" label={t("The replica server, as the primary server reaches it")}>
              <input id="rs-addr" className="text mono" placeholder={t("default: host/port of the replica server in fbadmin")} value={f.syncAddress} onChange={(e) => set("syncAddress", e.target.value)} />
            </Field>
            <Field id="rs-suser" label={t("User on the replica")}>
              <input id="rs-suser" className="text" placeholder={t("default: the replica server's user")} value={f.syncUser} onChange={(e) => set("syncUser", e.target.value)} />
            </Field>
            <Field id="rs-spw" label={t("Password")}>
              <input id="rs-spw" className="text" type="password" autoComplete="new-password" placeholder={t("default: the replica server's saved password")} value={f.syncPassword} onChange={(e) => set("syncPassword", e.target.value)} />
            </Field>
          </>}
          <span className="l">{t("The copy")}</span>
          <div className="rs-radios" role="radiogroup" aria-label={t("The copy")}>
            <label className="check"><input type="radio" name="rs-transfer" checked={f.transfer === "shared"} onChange={() => set("transfer", "shared")} /> {t("Both servers see the copy directory (same server, shared storage)")}</label>
            <label className="check"><input type="radio" name="rs-transfer" checked={f.transfer === "manual"} onChange={() => set("transfer", "manual")} /> {t("I copy the file to the replica server myself")}</label>
          </div>
          <Field id="rs-copydir" label={t("Copy directory (primary server)")}>
            <input id="rs-copydir" className="text mono" placeholder={resolved?.copy_directory ?? t("default: next to the database")} value={f.copyDir} onChange={(e) => set("copyDir", e.target.value)} />
          </Field>
          {f.transfer === "shared" && <Field id="rs-copydir2" label={t("The copy directory, as the replica server sees it")}>
            <input id="rs-copydir2" className="text mono" placeholder={resolved?.copy_directory_replica ?? t("default: the same path")} value={f.copyDirReplica} onChange={(e) => set("copyDirReplica", e.target.value)} />
          </Field>}
          <span />
          <div className="mt-row">
            <button className="btn primary" disabled={busy || !f.server || !f.path.trim() || !!job} onClick={doPlan}>{plan ? t("Check again") : t("Check the plan")}</button>
            {stale && <span className="warn">{t("Changed since the check")}</span>}
          </div>
        </div>
        {error && <div className="err">{error}</div>}
        {plan && <div className="alerts rs-checks">{plan.checks.map((c) => (
          <div key={c.code} className={"alert " + LEVEL[c.level]}><span className="lvl">{t(c.level)}</span><span>{c.message}</span></div>
        ))}</div>}
        {plan && !plan.ok && <div className="err">{t("{n} problem(s) to fix before the setup can start.", { n: errors })}</div>}
      </Section>

      {plan?.ok && <Section wide title={t("2. replication.conf of the primary server")}
        hint={async
          ? t("Add this block to replication.conf in the Firebird directory of the primary server, and create the journal and archive directories (writable by the Firebird server). The block is read when the database is opened: fbadmin closes its own connections when the setup starts; other applications must disconnect.")
          : t("Add this block to replication.conf in the Firebird directory of the primary server. It is read when the database is opened.")}>
        <ConfBlock id="rs-pconf" database={plan.primary.path} block={plan.conf.primary} written={primaryWritten} onWritten={setPrimaryWritten} />
        {async && resolved && <pre className="code-block rs-mkdir" tabIndex={0} aria-label={t("Commands for the directories")}>{`mkdir -p ${resolved.journal_directory} ${resolved.archive_directory}\nchown firebird:firebird ${resolved.journal_directory} ${resolved.archive_directory}`}</pre>}
        <div className="mt-row">
          <button className="btn small" onClick={async () => {
            try {
              const r = await sapi.reopen(serverId, dbId);
              setOthers(r.reopened ? t("fbadmin's connections are closed and no other application is attached.") : t("Still attached: {who}", { who: r.others.map((a) => `${a.user} (${a.process ?? a.address ?? "?"})`).join(", ") }));
            } catch (e) { app.toast(errMsg(e), "error"); }
          }}>{t("Close fbadmin's connections to the database")}</button>
          {others && <span className="muted">{others}</span>}
        </div>
      </Section>}

      {plan?.ok && <Section wide title={t("3. Copy and replica")}>
        <p className="muted">{async
          ? t("fbadmin enables publication, checks that the primary writes its journal, makes the copy, restores it on the replica server as a read-only replica and registers it.")
          : t("fbadmin makes the copy, restores it on the replica server as a read-only replica, registers it and then enables publication.")}</p>
        {!job && <button className="btn primary" disabled={!canRun} onClick={async () => {
          try { setJob(await sapi.run(serverId, dbId, body(f))); } catch (e) { app.toast(errMsg(e), "error"); }
        }}>{t("Start the setup")}</button>}
        {!primaryWritten && !job && <span className="muted"> {t("First add the primary's block (step 2).")}</span>}
        <JobLog job={job} onDone={onDone} />
        {result?.waiting_for_transfer && !replicaDb && <div className="rs-transfer">
          <p>{t("Copy {file} to the replica server, then enter where it is there:", { file: result.copy_file ?? "" })}</p>
          <div className="mt-row">
            <label htmlFor="rs-copyfile" className="muted">{t("The copy on the replica server")}</label>
            <input id="rs-copyfile" className="text mono rs-path" value={copyOnReplica} onChange={(e) => setCopyOnReplica(e.target.value)} />
            <button className="btn primary" disabled={!copyOnReplica.trim()} onClick={async () => {
              try { setResult(null); setJob(await sapi.restore(serverId, dbId, { ...body(f), copy_file: copyOnReplica.trim() })); }
              catch (e) { app.toast(errMsg(e), "error"); }
            }}>{t("Restore and continue")}</button>
          </div>
        </div>}
        {result?.status === "failed" && <button className="btn" onClick={() => { setJob(null); setResult(null); }}>{t("Try again")}</button>}
      </Section>}

      {replicaDb && plan?.conf.replica && <Section wide title={t("4. replication.conf of the replica server")}
        hint={t("Add this block to replication.conf of the replica server and restart that Firebird server: it reads replica blocks at startup. The archive directory must be readable there.")}>
        <ConfBlock id="rs-rconf" database={result!.replica_path} block={plan.conf.replica} written={replicaWritten} onWritten={setReplicaWritten} />
        <label className="check"><input type="checkbox" checked={restarted} onChange={(e) => setRestarted(e.target.checked)} /> {t("The replica's Firebird server was restarted")}</label>
      </Section>}

      {replicaDb && <Section wide title={(plan?.conf.replica ? "5. " : "4. ") + t("Check that changes arrive")}>
        <p className="muted">{t("fbadmin inserts a row into FBADMIN_REPLICATION_CHECK on the primary and waits until it is on the replica.")}</p>
        <div className="mt-row">
          <button className="btn primary" disabled={checking || (!!plan?.conf.replica && !restarted)} onClick={sendCheck}>{checking ? t("Waiting for the replica…") : t("Send a check row")}</button>
          <button className="btn" onClick={() => app.openTab({ kind: "replication", title: t("{name} · replication", { name: db?.name ?? dbId }), serverId, dbId })}>{t("Open replication status")}</button>
        </div>
        {check && (check.arrived
          ? <div className="rs-ok" role="status">✓ {t("Arrived on the replica after {n} s. Replication works.", { n: check.arrived_after ?? check.seconds })}</div>
          : <div className="rs-wait" role="status">
            {t("Not on the replica yet ({n} s).", { n: check.seconds })}{" "}
            {t("Primary sequence {p}, replica sequence {r}, replica mode {m}.", { p: check.primary_sequence ?? "–", r: check.replica_sequence ?? "–", m: check.replica_mode ?? "–" })}
            {!checking && <div className="muted">{async
              ? t("If it doesn't arrive: is the replica's block in its replication.conf and was that server restarted? Is the archive directory readable there? The replica server's replication.log has the reason.")
              : t("If it doesn't arrive: was the database reopened after the block was added (all attachments closed)? The primary server's replication.log has the reason.")}</div>}
          </div>)}
      </Section>}
    </div>
  );
}

/** Replication tab: send a check row to the chosen replica and wait for it. */
export function CheckButton(props: { serverId: string; dbId: string; peerServer: string; peerDb: string }) {
  const app = useApp();
  const [state, setState] = useState<CheckState | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setState(null), [props.peerServer, props.peerDb]);
  return (
    <div className="mt-row rs" style={{ marginTop: 8 }}>
      <button className="btn" disabled={busy} onClick={async () => {
        setBusy(true); setState(null);
        try {
          const sent = await sapi.check(props.serverId, props.dbId, props.peerServer, props.peerDb);
          const end = Date.now() + 120_000;
          while (Date.now() < end) {
            const c = await sapi.poll(props.serverId, props.dbId, sent.token).catch(() => null);
            if (c) setState(c);
            if (c?.arrived) break;
            await new Promise((r) => setTimeout(r, 1500));
          }
        } catch (e) { app.toast(errMsg(e), "error"); } finally { setBusy(false); }
      }}>{busy ? t("Waiting for the replica…") : t("Send a check row")}</button>
      {state && (state.arrived
        ? <span className="ok" role="status">✓ {t("Arrived on the replica after {n} s. Replication works.", { n: state.arrived_after ?? state.seconds })}</span>
        : <span role="status">{t("Not on the replica yet ({n} s).", { n: state.seconds })}</span>)}
    </div>
  );
}

export function openReplicationSetup(app: AppApi, sid: string, dbid: string) {
  const name = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? dbid;
  app.openTab({ kind: "replication-setup", title: t("{name} · replica setup", { name }), serverId: sid, dbId: dbid });
}

registerTabKind("replication-setup", {
  icon: <span style={{ fontSize: 12 }}>⇉</span>,
  render: (tab, visible) => <SetupTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app, sel) => {
  if (sel.kind !== "database" || !sel.dbId || app.servers.find((s) => s.id === sel.serverId)?.embedded) return [];
  return [{ label: t("Set up a replica…"), onClick: () => openReplicationSetup(app, sel.serverId, sel.dbId!) }];
});

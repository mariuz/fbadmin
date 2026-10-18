import { useEffect, useMemo, useState } from "react";
import { errMsg, keys, useApp } from "../../context";
import { t } from "../../i18n";
import type { Job } from "../../types";
import { IconRefresh } from "../../components/Icons";
import { mapi, type DbState, type LimboTx } from "./api";
import { Meter } from "./charts";
import { Check, JobLog, Section, numOrNull, useBusy, useCaps } from "./common";

interface P { serverId: string; dbId: string }

function useDb(p: P) {
  const app = useApp();
  return app.servers.find((s) => s.id === p.serverId)?.databases.find((d) => d.id === p.dbId);
}

export function StatePills(props: { state: DbState | null }) {
  const h = props.state?.header;
  if (!h) return null;
  return (
    <span className="mt-row">
      <span className={"state-pill " + h.shutdown_mode}>{h.shutdown_mode === "online" ? t("online") : t("{mode} shutdown", { mode: h.shutdown_mode })}</span>
      {h.backup_state !== "normal" && <span className={"state-pill " + h.backup_state}>{t("nbackup {state}", { state: h.backup_state })}</span>}
      {h.read_only && <span className="state-pill">{t("read-only")}</span>}
      <span className="state-pill">{h.forced_writes ? t("forced writes") : t("async writes")}</span>
      {h.encrypted && <span className="state-pill">{t("encrypted")}</span>}
    </span>
  );
}

function useDbState(p: P) {
  const [state, setState] = useState<DbState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => mapi.properties(p.serverId, p.dbId).then((s) => { setState(s); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, [p.serverId, p.dbId]);
  return { state, setState, error, load };
}

// ----------------------------------------------------------------- nbackup
export function NbackupSection(p: P) {
  const app = useApp();
  const db = useDb(p)!;
  const stem = db.path.replace(/\.(fdb|gdb)$/i, "");
  const [level, setLevel] = useState(0);
  const [file, setFile] = useState(`${stem}.nbk0`);
  const [direct, setDirect] = useState<"" | "on" | "off">("");
  const [noTriggers, setNoTriggers] = useState(false);
  const [history, setHistory] = useState<Array<Record<string, any>>>([]);
  const [chain, setChain] = useState("");
  const [target, setTarget] = useState(`${stem}_nrestored.fdb`);
  const [registerAs, setRegisterAs] = useState("");
  const [fixPath, setFixPath] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const { state, setState, load } = useDbState(p);
  const caps = useCaps(p.serverId);
  const { busy, error, run } = useBusy();
  const loadHistory = () => mapi.nbackupHistory(p.serverId, p.dbId).then((h) => {
    setHistory(h);
    if (!chain) setChain(h.map((x) => x.file).join("\n"));
  }).catch(() => undefined);
  useEffect(() => { loadHistory(); }, []);
  useEffect(() => { setFile(`${stem}.nbk${level}`); }, [level]);
  return (
    <>
      <Section title={t("Incremental backup (nbackup)")} hint={t("Files are written on the Firebird server. Level 0 is a full copy; level N stores pages changed since the last level N−1.")}
        right={<StatePills state={state} />}>
        <div className="form mform">
          <label className="l">{t("Level")}</label>
          <input className="text" type="number" min={0} value={level} onChange={(e) => setLevel(Number(e.target.value))} />
          <label className="l">{t("Backup file (server)")}</label>
          <input className="text" value={file} onChange={(e) => setFile(e.target.value)} />
          <label className="l">{t("Direct I/O")}</label>
          <select className="text" value={direct} onChange={(e) => setDirect(e.target.value as any)}>
            <option value="">{t("(server default)")}</option><option value="on">ON</option><option value="off">OFF</option>
          </select>
          <span />
          <Check label={t("Do not run database triggers")} checked={noTriggers} onChange={setNoTriggers} />
        </div>
        <div className="mt-row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => {
            setJob(await mapi.nbackup(p.serverId, p.dbId, { level, file, no_triggers: noTriggers, direct: direct === "" ? null : direct === "on" }));
          })}>{t("Start nbackup")}</button>
          <span className="muted">{t("Lock for a file-system copy:")}</span>
          <button className="btn" disabled={busy} onClick={() => run(async () => setState(await mapi.nbackupLock(p.serverId, p.dbId)), t("Database locked (BEGIN BACKUP)"))}>{t("Lock")}</button>
          <button className="btn" disabled={busy} onClick={() => run(async () => setState(await mapi.nbackupUnlock(p.serverId, p.dbId)), t("Database unlocked (END BACKUP)"))}>{t("Unlock")}</button>
          <button className="btn icon" title={t("Refresh state")} onClick={() => { load(); loadHistory(); }}><IconRefresh /></button>
        </div>
        {error && <div className="err" style={{ marginTop: 8 }}>{error}</div>}
        <JobLog job={job} onDone={loadHistory} />
        <h3>{t("Backup history (RDB$BACKUP_HISTORY)")}</h3>
        {history.length === 0 ? <div className="muted">{t("No nbackup history.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>#</th><th>{t("Level")}</th><th>{t("Time")}</th><th>SCN</th><th>{t("File")}</th></tr></thead>
            <tbody>{history.map((h) => <tr key={h.id}><td className="num">{h.id}</td><td className="num">{h.level}</td><td>{String(h.timestamp).replace("T", " ").slice(0, 19)}</td><td className="num">{h.scn}</td><td>{h.file}</td></tr>)}</tbody>
          </table></div>
        )}
      </Section>
      <Section title={t("Restore a backup chain")} hint={t("List the level 0, 1, 2… files in order (one per line). The chain is merged into a new database on the server.")}>
        <div className="form mform">
          <label className="l">{t("Files (in order)")}</label>
          <textarea className="text mono" rows={4} value={chain} onChange={(e) => setChain(e.target.value)} />
          <label className="l">{t("Target database")}</label>
          <input className="text" value={target} onChange={(e) => setTarget(e.target.value)} />
          <label className="l">{t("Register as")}</label>
          <input className="text" placeholder={t("optional display name")} value={registerAs} onChange={(e) => setRegisterAs(e.target.value)} />
        </div>
        <div className="mt-row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy || !chain.trim() || !target} onClick={() => run(async () => {
            const j = await mapi.nrestore(p.serverId, { files: chain.split("\n").map((s) => s.trim()).filter(Boolean), database_path: target, register_as: registerAs || null });
            setJob(j);
            if (registerAs) setTimeout(() => { app.reloadServers(); app.invalidate(keys.server(p.serverId)); }, 2500);
          })}>{t("Restore chain")}</button>
        </div>
      </Section>
      <Section title={t("Fixup (nbackup -F)")} hint={t("Marks a database file that was copied while locked as a normal database. Firebird 4+ (Services API).")}>
        <div className="mt-row">
          <input className="text" style={{ flex: 1 }} placeholder={t("{path} (this database)", { path: db.path })} value={fixPath} onChange={(e) => setFixPath(e.target.value)} />
          <button className="btn" disabled={busy || caps?.features.nfix === false} onClick={() => run(async () => {
            await mapi.nbackupFixup(p.serverId, p.dbId, fixPath);
            load();
          }, t("Fixup completed"))}>{t("Fixup")}</button>
          {caps?.features.nfix === false && <span className="muted">{t("needs Firebird 4+")}</span>}
        </div>
      </Section>
    </>
  );
}

// -------------------------------------------------------------------- gfix
export function RepairSection(p: P) {
  const caps = useCaps(p.serverId);
  const [mode, setMode] = useState("online");
  const [tInc, setTInc] = useState("");
  const [tExc, setTExc] = useState("");
  const [iInc, setIInc] = useState("");
  const [iExc, setIExc] = useState("");
  const [lockTimeout, setLockTimeout] = useState("");
  const [workers, setWorkers] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [limbo, setLimbo] = useState<LimboTx[] | null>(null);
  const { busy, error, run } = useBusy();
  const app = useApp();
  const loadLimbo = () => mapi.limbo(p.serverId, p.dbId).then(setLimbo).catch((e) => app.toast(errMsg(e), "error"));
  useEffect(() => { loadLimbo(); }, []);
  return (
    <>
      <Section title={t("Validate")} hint={t("Online validation runs while users are connected (Firebird 3+). Full / read-only / mend need exclusive access.")}>
        <div className="form mform">
          <label className="l">{t("Mode")}</label>
          <select className="text" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="online">{t("Online validation (gfix -v online)")}</option>
            <option value="full">{t("Full validation (gfix -v -full)")}</option>
            <option value="check">{t("Read-only check (gfix -v -n)")}</option>
            <option value="mend">{t("Mend (gfix -mend) — prepares a corrupt database for backup")}</option>
          </select>
          {mode === "online" && <>
            <label className="l">{t("Tables include / exclude")}</label>
            <div className="mt-row">
              <input className="text" style={{ flex: 1 }} placeholder={t("SIMILAR TO pattern")} value={tInc} onChange={(e) => setTInc(e.target.value)} />
              <input className="text" style={{ flex: 1 }} placeholder={t("exclude pattern")} value={tExc} onChange={(e) => setTExc(e.target.value)} />
            </div>
            <label className="l">{t("Indices include / exclude")}</label>
            <div className="mt-row">
              <input className="text" style={{ flex: 1 }} placeholder={t("SIMILAR TO pattern")} value={iInc} onChange={(e) => setIInc(e.target.value)} />
              <input className="text" style={{ flex: 1 }} placeholder={t("exclude pattern")} value={iExc} onChange={(e) => setIExc(e.target.value)} />
            </div>
            <label className="l">{t("Lock timeout (s)")}</label>
            <input className="text" type="number" placeholder="10" value={lockTimeout} onChange={(e) => setLockTimeout(e.target.value)} />
          </>}
        </div>
        <div className="mt-row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => setJob(await mapi.validate(p.serverId, p.dbId, {
            mode, tables_include: tInc || null, tables_exclude: tExc || null, indices_include: iInc || null,
            indices_exclude: iExc || null, lock_timeout: numOrNull(lockTimeout),
          })))}>{t("Validate")}</button>
        </div>
      </Section>
      <Section title={t("Sweep and ICU")} hint={t("Sweep removes garbage record versions and advances the oldest interesting transaction. ICU fix rebuilds indices after an ICU library change (gfix -icu).")}>
        <div className="mt-row">
          <input className="text" type="number" min={1} style={{ width: 160 }} disabled={!caps?.features.parallel_workers}
            placeholder={caps?.features.parallel_workers ? t("parallel workers") : t("workers: Firebird 5+")} value={workers} onChange={(e) => setWorkers(e.target.value)} />
          <button className="btn primary" disabled={busy} onClick={() => run(async () => setJob(await mapi.sweep(p.serverId, p.dbId, numOrNull(workers))))}>{t("Sweep now")}</button>
          <button className="btn" disabled={busy} onClick={() => run(async () => setJob(await mapi.icu(p.serverId, p.dbId)))}>{t("Fix ICU indices")}</button>
        </div>
      </Section>
      {error && <div className="err" style={{ marginBottom: 10 }}>{error}</div>}
      <JobLog job={job} />
      <Section title={t("Limbo transactions")} hint={t("Two-phase commit transactions left in limbo (prepared, never committed or rolled back) block garbage collection.")}
        right={<button className="btn small" onClick={loadLimbo}><IconRefresh size={12} /> {t("Refresh")}</button>}>
        {limbo === null ? <div className="muted">{t("Loading…")}</div> : limbo.length === 0 ? <div className="ok">{t("No limbo transactions.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Transaction")}</th><th>{t("Type")}</th><th>{t("State")}</th><th>{t("Advice")}</th><th>{t("Host / remote")}</th><th /></tr></thead>
            <tbody>{limbo.map((tx) => (
              <tr key={tx.id}>
                <td className="num">{tx.id}</td><td>{tx.multi ? t("multi-database") : t("single")}</td><td>{tx.state ?? t("limbo")}</td>
                <td>{tx.advise ?? "—"}</td><td>{[tx.host_site, tx.remote_site, tx.db_path].filter(Boolean).join(" · ") || "—"}</td>
                <td>
                  {(["commit", "rollback"] as const).map((a) => (
                    <button key={a} className="btn small" style={{ marginRight: 4 }} disabled={busy} onClick={() => run(async () => {
                      const r = await mapi.resolveLimbo(p.serverId, p.dbId, tx.id, a);
                      setLimbo(r.limbo);
                    }, t("Transaction {id}: {action}", { id: tx.id, action: a }))}>{a === "commit" ? t("Commit") : t("Roll back")}</button>
                  ))}
                </td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Section>
    </>
  );
}

// ---------------------------------------------------------- shutdown/online
export function ShutdownSection(p: P) {
  const { state, setState, error: loadErr, load } = useDbState(p);
  const [mode, setMode] = useState("full");
  const [method, setMethod] = useState("force");
  const [timeout, setTimeout_] = useState("0");
  const [onlineMode, setOnlineMode] = useState("normal");
  const { busy, error, run } = useBusy();
  const app = useApp();
  const merge = (h: DbState["header"]) => setState((s) => (s ? { ...s, header: h } : { header: h, engine_major: 0, database: null, error: null }));
  return (
    <>
      <Section title={t("Current state")} right={<button className="btn small" onClick={load}><IconRefresh size={12} /> {t("Refresh")}</button>}>
        {loadErr ? <div className="err">{loadErr}</div> : <StatePills state={state} />}
        <div className="hint" style={{ marginTop: 8 }}>
          {t("multi: only SYSDBA/owner may connect · single: one SYSDBA/owner connection · full: no connections at all (only Services API maintenance).")}
        </div>
      </Section>
      <Section title={t("Shut down")} hint={t("force: disconnect everyone after the timeout · attachments: wait until all attachments are gone (fail after timeout) · transactions: wait for running transactions.")}>
        <div className="form mform">
          <label className="l">{t("Mode")}</label>
          <select className="text" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="multi">{t("Multi-user maintenance")}</option><option value="single">{t("Single-user maintenance")}</option><option value="full">{t("Full shutdown")}</option>
          </select>
          <label className="l">{t("Method")}</label>
          <select className="text" value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="force">{t("Force (-force)")}</option><option value="attachments">{t("Deny new attachments (-attach)")}</option><option value="transactions">{t("Deny new transactions (-tran)")}</option>
          </select>
          <label className="l">{t("Timeout (seconds)")}</label>
          <input className="text" type="number" min={0} value={timeout} onChange={(e) => setTimeout_(e.target.value)} />
        </div>
        <div className="mt-row" style={{ marginTop: 10 }}>
          <button className="btn danger solid" disabled={busy} onClick={() => run(async () => {
            if (!confirm(t("Shut down the database ({mode}, {method}, {timeout}s)? Connected users may be disconnected.", { mode, method, timeout }))) return;
            merge(await mapi.shutdown(p.serverId, p.dbId, { mode, method, timeout: Number(timeout) || 0 }));
            app.invalidate(keys.db(p.serverId, p.dbId));
          }, t("Shutdown completed"))}>{t("Shut down")}</button>
        </div>
      </Section>
      <Section title={t("Bring online")} hint={t("normal: everyone may connect. You can also move from full to single or multi maintenance.")}>
        <div className="mt-row">
          <select className="text" value={onlineMode} onChange={(e) => setOnlineMode(e.target.value)}>
            <option value="normal">{t("Normal (online)")}</option><option value="multi">{t("Multi-user maintenance")}</option><option value="single">{t("Single-user maintenance")}</option>
          </select>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => {
            merge(await mapi.online(p.serverId, p.dbId, onlineMode));
            app.invalidate(keys.db(p.serverId, p.dbId));
          }, t("Database is online"))}>{t("Bring online")}</button>
        </div>
      </Section>
      {error && <div className="err" style={{ whiteSpace: "pre-wrap" }}>{error}</div>}
    </>
  );
}

// -------------------------------------------------------------- properties
const CHARSETS = ["NONE", "UTF8", "UNICODE_FSS", "ASCII", "WIN1250", "WIN1251", "WIN1252", "WIN1253", "WIN1254", "WIN1257",
  "ISO8859_1", "ISO8859_2", "ISO8859_15", "KOI8R", "KOI8U", "DOS850", "DOS866", "SJIS_0208", "GBK", "EUCJ_0208", "BIG_5"];

export function PropertiesSection(p: P) {
  const { state, setState, error: loadErr, load } = useDbState(p);
  const [f, setF] = useState<Record<string, string>>({});
  const [changed, setChanged] = useState<string[]>([]);
  const { busy, error, run } = useBusy();
  const app = useApp();
  const cur = useMemo(() => {
    const h = state?.header;
    const d = state?.database;
    if (!h) return null;
    return {
      page_buffers: String(d?.page_buffers ?? h.page_buffers ?? 0),
      sweep_interval: String(d?.sweep_interval ?? h.sweep_interval ?? ""),
      forced_writes: h.forced_writes ? "1" : "0",
      reserve_space: h.reserve_space ? "1" : "0",
      read_only: h.read_only ? "1" : "0",
      dialect: String(d?.sql_dialect ?? h.database_dialect ?? 3),
      linger: String(d?.linger ?? ""),
      default_charset: String(d?.default_charset ?? ""),
    };
  }, [state]);
  useEffect(() => { if (cur) setF(cur); }, [cur]);
  if (loadErr) return <div className="err">{loadErr}</div>;
  if (!cur) return <div className="muted">{t("Loading…")}</div>;
  const diff: Record<string, unknown> = {};
  for (const k of Object.keys(cur) as Array<keyof typeof cur>) {
    if (f[k] === undefined || f[k] === cur[k]) continue;
    diff[k] = ["forced_writes", "reserve_space", "read_only"].includes(k) ? f[k] === "1"
      : ["default_charset"].includes(k) ? f[k] : Number(f[k]);
  }
  const set = (k: string) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const yn = (k: string, on: string, off: string) => (
    <select className="text" value={f[k]} onChange={set(k)}><option value="1">{on}</option><option value="0">{off}</option></select>
  );
  const d = state?.database;
  return (
    <Section title={t("Database properties")} right={<><StatePills state={state} /><button className="btn small" onClick={load}><IconRefresh size={12} /></button></>}
      hint={t("Page buffers, sweep interval, write mode, reserve space, dialect and access mode are set through the Services API (gfix); linger and the default character set with ALTER DATABASE. Fbadmin's own connections to the database are closed first.")}>
      {state?.error && <div className="warn" style={{ marginBottom: 8 }}>{state.error}</div>}
      <div className="form mform">
        <label className="l">{t("Page buffers")}</label>
        <input className="text" type="number" min={0} value={f.page_buffers ?? ""} onChange={set("page_buffers")} />
        <label className="l">{t("Sweep interval")}</label>
        <input className="text" type="number" min={0} value={f.sweep_interval ?? ""} onChange={set("sweep_interval")} />
        <label className="l">{t("Write mode")}</label>{yn("forced_writes", t("Forced writes (sync)"), t("Asynchronous"))}
        <label className="l">{t("Reserve space")}</label>{yn("reserve_space", t("Reserve space for back versions"), t("Use full pages (no reserve)"))}
        <label className="l">{t("Access mode")}</label>{yn("read_only", t("Read-only"), t("Read-write"))}
        <label className="l">{t("SQL dialect")}</label>
        <select className="text" value={f.dialect} onChange={set("dialect")}><option value="3">3</option><option value="1">1</option></select>
        <label className="l">{t("Linger (seconds)")}</label>
        <input className="text" type="number" min={0} disabled={!d} value={f.linger ?? ""} onChange={set("linger")} />
        <label className="l">{t("Default character set")}</label>
        <select className="text" disabled={!d} value={f.default_charset} onChange={set("default_charset")}>
          {[...new Set([f.default_charset, ...CHARSETS])].filter(Boolean).map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <div className="mt-row" style={{ marginTop: 12 }}>
        <button className="btn primary" disabled={busy || Object.keys(diff).length === 0} onClick={() => run(async () => {
          const r = await mapi.setProperties(p.serverId, p.dbId, diff);
          setChanged(r.changed);
          setState(r.state);
          app.invalidate(keys.db(p.serverId, p.dbId));
        }, t("Properties saved"))}>{t("Apply")} {Object.keys(diff).length ? `(${Object.keys(diff).length})` : ""}</button>
        <button className="btn" disabled={busy} onClick={() => cur && setF(cur)}>{t("Reset")}</button>
        {error && <span className="err" style={{ whiteSpace: "pre-wrap" }}>{error}</span>}
      </div>
      {changed.length > 0 && <ul className="ok">{changed.map((c) => <li key={c}>{c}</li>)}</ul>}
      <h3>{t("Header page")}</h3>
      <table className="kv"><tbody>
        {Object.entries(state?.header ?? {}).filter(([, v]) => typeof v !== "object").map(([k, v]) => (
          <tr key={k}><td>{k.replace(/_/g, " ")}</td><td>{String(v)}</td></tr>
        ))}
        <tr><td>{t("attributes")}</td><td>{state?.header.attributes.join(", ") || "—"}</td></tr>
      </tbody></table>
    </Section>
  );
}

// ---------------------------------------------------------------- encryption
export function EncryptionSection(p: P) {
  const [st, setSt] = useState<Record<string, any> | null>(null);
  const [plugin, setPlugin] = useState("");
  const [key, setKey] = useState("");
  const { busy, error, run } = useBusy();
  const load = () => mapi.cryptState(p.serverId, p.dbId).then(setSt).catch(() => undefined);
  useEffect(() => { load(); }, []);
  const inProgress = st && (st.crypt_state === 2 || st.crypt_state === 3 || (st.crypt_page ?? 0) > 0);
  useEffect(() => {
    if (!inProgress) return;
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [inProgress]);
  return (
    <Section title={t("Database encryption")} hint={t("ALTER DATABASE ENCRYPT WITH <plugin> [KEY <name>] starts background encryption on the server; progress comes from MON$DATABASE (MON$CRYPT_PAGE / MON$CRYPT_STATE). A crypt plugin must be installed and configured on the server.")}
      right={<button className="btn small" onClick={load}><IconRefresh size={12} /></button>}>
      {st && (
        <table className="kv" style={{ marginBottom: 10 }}><tbody>
          <tr><td>{t("State")}</td><td>{st.supported_state ? st.crypt_state_name : t("(MON$CRYPT_STATE needs Firebird 4+)")}</td></tr>
          <tr><td>{t("Progress")}</td><td><Meter value={st.progress} label={t("{page} of {pages} pages ({progress}%)", { page: st.crypt_page ?? 0, pages: st.pages ?? "?", progress: st.progress ?? 0 })} /></td></tr>
        </tbody></table>
      )}
      <div className="form mform">
        <label className="l">{t("Crypt plugin")}</label>
        <input className="text" placeholder={t("e.g. DbCrypt_example")} value={plugin} onChange={(e) => setPlugin(e.target.value)} />
        <label className="l">{t("Key name")}</label>
        <input className="text" placeholder={t("optional")} value={key} onChange={(e) => setKey(e.target.value)} />
      </div>
      <div className="mt-row" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={busy || !plugin.trim()} onClick={() => run(async () => setSt(await mapi.encrypt(p.serverId, p.dbId, plugin, key)), t("Encryption started"))}>{t("Encrypt")}</button>
        <button className="btn" disabled={busy} onClick={() => run(async () => setSt(await mapi.decrypt(p.serverId, p.dbId)), t("Decryption started"))}>{t("Decrypt")}</button>
      </div>
      {error && <div className="err" style={{ marginTop: 8, whiteSpace: "pre-wrap" }}>{error}</div>}
    </Section>
  );
}

// ------------------------------------------------------------------- config
export function ConfigSection(p: P) {
  const [cfg, setCfg] = useState<Awaited<ReturnType<typeof mapi.config>> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [onlySet, setOnlySet] = useState(false);
  useEffect(() => { mapi.config(p.serverId, p.dbId).then(setCfg).catch((e) => setErr(errMsg(e))); }, []);
  if (err) return <div className="err">{err}</div>;
  if (!cfg) return <div className="muted">{t("Loading…")}</div>;
  if (!cfg.available) return <div className="empty-state"><div className="big">{t("Configuration viewer not available")}</div><div>{cfg.message}</div></div>;
  const q = filter.toLowerCase();
  const rows = cfg.rows.filter((r) => (!onlySet || r.is_set) && (!q || r.name.toLowerCase().includes(q) || String(r.value ?? "").toLowerCase().includes(q)));
  return (
    <Section title="firebird.conf / databases.conf (RDB$CONFIG)" hint={t("Effective configuration for this database, read-only. “Source” names the file a value was set in (firebird.conf or databases.conf).")}>
      <div className="mt-row" style={{ marginBottom: 8 }}>
        <input className="text" placeholder={t("Filter by name or value")} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <Check label={t("Only values set explicitly")} checked={onlySet} onChange={setOnlySet} />
        <span className="muted">{t("{shown} of {total}", { shown: rows.length, total: cfg.rows.length })}</span>
      </div>
      <div className="grid-wrap" style={{ maxHeight: "60vh" }}><table className="grid">
        <thead><tr><th>{t("Name")}</th><th>{t("Value")}</th><th>{t("Default")}</th><th>{t("Source")}</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.id}><td style={r.is_set ? { fontWeight: 600 } : undefined}>{r.name}</td><td>{r.value ?? "—"}</td><td className="muted">{r.default ?? "—"}</td><td>{r.source ?? (r.is_set ? "" : t("default"))}</td></tr>
        ))}</tbody>
      </table></div>
    </Section>
  );
}

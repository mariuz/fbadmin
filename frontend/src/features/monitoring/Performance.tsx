import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import { IconCopy, IconPlay, IconRefresh, IconTerminal } from "../../components/Icons";
import { SqlEditor } from "../../components/SqlEditor";
import { LineChart, type Series } from "./LineChart";
import { N_, t } from "../../i18n";
import { fmtAge, fmtBytes, fmtNum, fmtTs, monApi, type Features, type MonSettings, type Sample, type Snapshot } from "./api";

type Props = { serverId: string; dbId: string; visible: boolean };

const SUBTABS = [
  ["live", N_("Live")],
  ["history", N_("History")],
  ["tables", N_("Table statistics")],
  ["transactions", N_("Transactions & OAT")],
  ["locks", N_("Locks")],
  ["compiled", N_("Statement cache")],
  ["advisor", N_("Index advisor")],
  ["settings", N_("Settings")],
] as const;
type Sub = (typeof SUBTABS)[number][0];

export function PerformanceTab(props: Props & { initial?: string }) {
  const [sub, setSub] = useState<Sub>((props.initial as Sub) ?? "live");
  const [features, setFeatures] = useState<Features | null>(null);
  useEffect(() => { monApi.features(props.serverId, props.dbId).then(setFeatures).catch(() => {}); }, [props.serverId, props.dbId]);
  return (
    <div className="mon">
      <div className="subtabs">
        {SUBTABS.map(([k, label]) => (
          <button key={k} className={sub === k ? "active" : ""} onClick={() => setSub(k)}>{t(label)}</button>
        ))}
        <span style={{ flex: 1 }} />
        {features && <span className="muted" style={{ alignSelf: "center", fontSize: 12 }}>{t("Firebird {version}", { version: features.engine_version })}</span>}
      </div>
      <div className="mon-body">
        {sub === "live" && <LivePanel {...props} />}
        {sub === "history" && <HistoryPanel {...props} />}
        {sub === "tables" && <TablesPanel {...props} />}
        {sub === "transactions" && <TransactionsPanel {...props} />}
        {sub === "locks" && <LocksPanel {...props} />}
        {sub === "compiled" && <CompiledPanel {...props} />}
        {sub === "advisor" && <AdvisorPanel {...props} />}
        {sub === "settings" && <SettingsPanel />}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ charts shared by live + history
interface Point { ts: number; snap: Snapshot; rates: Record<string, number | null> }

function rates(prev: Snapshot | null, cur: Snapshot): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  const keys = ["next_transaction", "page_reads", "page_writes", "page_fetches", "page_marks", "seq_reads", "idx_reads",
    "inserts", "updates", "deletes", "backouts", "purges", "expunges", "record_waits", "record_conflicts"];
  for (const k of keys) {
    if (!prev) { out[k] = null; continue; }
    const a = prev[k], b = cur[k];
    const dt = cur.ts - prev.ts || 1e-9;
    out[k] = a === null || b === null || a === undefined || b === undefined || b < a ? null : (b - a) / dt;
  }
  return out;
}

const perSec = (v: number | null) => (v === null ? "–" : v < 10 ? v.toFixed(1) : fmtNum(v));

function ChartGrid(props: { points: Point[]; loading?: boolean; longRange?: boolean }) {
  const p = props.points;
  const times = p.map((x) => x.ts);
  const g = (k: string): (number | null)[] => p.map((x) => (x.snap[k] ?? null) as number | null);
  const r = (k: string): (number | null)[] => p.map((x) => x.rates[k] ?? null);
  const gap = p.map((x) => (x.snap.next_transaction != null && x.snap.oat != null ? x.snap.next_transaction - x.snap.oat : null));
  const common = { times, loading: props.loading, longRange: props.longRange };
  const s = (key: string, label: string, slot: 1 | 2 | 3, values: (number | null)[]): Series => ({ key, label, slot, values });
  return (
    <div className="mon-charts">
      <LineChart {...common} title={t("Attachments")} series={[s("att", t("Attachments"), 1, g("attachments"))]} format={(v) => fmtNum(v)} />
      <LineChart {...common} title={t("Transactions")} unit={t("per second")} series={[s("tps", t("Transactions/s"), 1, r("next_transaction"))]} format={perSec} />
      <LineChart {...common} title={t("Page I/O")} unit={t("pages per second")} series={[s("pr", t("Reads"), 1, r("page_reads")), s("pw", t("Writes"), 2, r("page_writes"))]} format={perSec} />
      <LineChart {...common} title={t("Page cache")} unit={t("per second")} series={[s("pf", t("Fetches"), 1, r("page_fetches")), s("pm", t("Marks"), 2, r("page_marks"))]} format={perSec} />
      <LineChart {...common} title={t("Record reads")} unit={t("records per second")} series={[s("sr", t("Sequential (full scan)"), 1, r("seq_reads")), s("ir", t("Indexed"), 2, r("idx_reads"))]} format={perSec} />
      <LineChart {...common} title={t("Record changes")} unit={t("records per second")} series={[s("ins", t("Inserts"), 1, r("inserts")), s("upd", t("Updates"), 2, r("updates")), s("del", t("Deletes"), 3, r("deletes"))]} format={perSec} />
      <LineChart {...common} title={t("Garbage")} unit={t("records per second")} series={[s("bo", t("Backouts"), 1, r("backouts")), s("pu", t("Purges"), 2, r("purges")), s("ex", t("Expunges"), 3, r("expunges"))]} format={perSec} />
      <LineChart {...common} binary title={t("Memory")} series={[s("mu", t("Used"), 1, g("memory_used")), s("ma", t("Allocated"), 2, g("memory_allocated"))]} format={(v) => fmtBytes(v)} />
      <LineChart {...common} title={t("Activity")} series={[s("tx", t("Open transactions"), 1, g("transactions")), s("st", t("Running statements"), 2, g("active_statements"))]} format={(v) => fmtNum(v)} />
      <LineChart {...common} title={t("Next transaction − OAT")} unit={t("transactions")} series={[s("gap", t("Gap"), 1, gap)]} format={(v) => fmtNum(v)} />
      <LineChart {...common} title={t("Lock contention")} unit={t("per second")} series={[s("rw", t("Record waits"), 1, r("record_waits")), s("rc", t("Update conflicts"), 2, r("record_conflicts"))]} format={perSec} />
    </div>
  );
}

function Tiles(props: { last: Point | undefined }) {
  const p = props.last;
  if (!p) return null;
  const fetches = p.rates.page_fetches, reads = p.rates.page_reads;
  const hit = fetches && fetches > 0 && reads !== null ? Math.max(0, 1 - reads / fetches) * 100 : null;
  const gap = (p.snap.next_transaction ?? 0) - (p.snap.oat ?? 0);
  return (
    <div className="cards" style={{ marginBottom: 12 }}>
      <div className="card"><div className="k">{t("Attachments")}</div><div className="v">{fmtNum(p.snap.attachments)}</div></div>
      <div className="card"><div className="k">{t("Transactions / s")}</div><div className="v">{perSec(p.rates.next_transaction)}</div></div>
      <div className="card"><div className="k">{t("Cache hit ratio")}</div><div className="v">{hit === null ? "–" : hit.toFixed(1) + " %"}</div></div>
      <div className="card"><div className="k">{t("Next − OAT")}</div><div className="v">{fmtNum(gap)}</div></div>
      <div className="card"><div className="k">{t("Memory used")}</div><div className="v">{fmtBytes(p.snap.memory_used)}</div></div>
      <div className="card"><div className="k">{t("Running statements")}</div><div className="v">{fmtNum(p.snap.active_statements)}</div></div>
    </div>
  );
}

// ------------------------------------------------------------------ live
function LivePanel(props: Props) {
  const [points, setPoints] = useState<Point[]>([]);
  const [interval, setIntervalSec] = useState(2);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const prev = useRef<Snapshot | null>(null);

  const poll = async () => {
    try {
      const snap = await monApi.snapshot(props.serverId, props.dbId);
      const pt = { ts: snap.ts, snap, rates: rates(prev.current, snap) };
      prev.current = snap;
      setPoints((p) => [...p.slice(-179), pt]);
      setError(null);
    } catch (e) { setError(errMsg(e)); }
  };
  useEffect(() => { poll(); }, []);
  useEffect(() => {
    if (paused || !props.visible) return;
    const t = setInterval(poll, interval * 1000);
    return () => clearInterval(t);
  }, [interval, paused, props.visible]);

  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px", borderTop: 0 }}>
        <label>{t("Refresh every")}
          <select className="text" value={interval} onChange={(e) => setIntervalSec(Number(e.target.value))}>
            {[1, 2, 5, 10, 30].map((s) => <option key={s} value={s}>{t("{n} s", { n: s })}</option>)}
          </select>
        </label>
        <button className={"btn" + (paused ? " on" : "")} onClick={() => setPaused((v) => !v)}>{paused ? t("Resume") : t("Pause")}</button>
        <button className="btn" onClick={() => { prev.current = null; setPoints([]); poll(); }}>{t("Reset")}</button>
        <span className="muted">{t("Database-wide MON$ counters since the database was opened; rates are deltas between samples.")}</span>
        {error && <span className="err">{error}</span>}
      </div>
      <Tiles last={points[points.length - 1]} />
      <ChartGrid points={points} />
    </>
  );
}

// ------------------------------------------------------------------ history
const RANGES: [number, string][] = [[1, N_("1 h")], [6, N_("6 h")], [24, N_("24 h")], [72, N_("3 d")], [168, N_("7 d")]];

function HistoryPanel(props: Props) {
  const app = useApp();
  const [hours, setHours] = useState(6);
  const [data, setData] = useState<{ tracking: any; settings: MonSettings; samples: Sample[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = () => {
    setLoading(true);
    monApi.history(props.serverId, props.dbId, hours).then((d) => { setData(d); setError(null); })
      .catch((e) => setError(errMsg(e))).finally(() => setLoading(false));
  };
  useEffect(load, [hours]);
  useEffect(() => {
    if (!props.visible || !data?.tracking) return;
    const t = setInterval(load, Math.max(15, data.settings.interval) * 1000);
    return () => clearInterval(t);
  }, [props.visible, data?.tracking, hours]);
  const points: Point[] = useMemo(() => (data?.samples ?? []).map((s) => ({ ts: s.ts, snap: s, rates: s.rates })), [data]);
  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try { await fn(); if (msg) app.toast(msg); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const tracking = data?.tracking;
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <label className="check">
          <input type="checkbox" checked={!!tracking} onChange={(e) => act(() => monApi.setTracking(props.serverId, props.dbId, e.target.checked),
            e.target.checked ? t("History recording enabled") : t("History recording disabled"))} />
          {t("Record history for this database")}
        </label>
        <span className="sep" />
        <div className="seg">
          {RANGES.map(([h, l]) => <button key={h} className={hours === h ? "on" : ""} onClick={() => setHours(h)}>{t(l)}</button>)}
        </div>
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        <button className="btn" disabled={!tracking} onClick={() => act(() => monApi.sampleNow(props.serverId, props.dbId))}>{t("Sample now")}</button>
        <button className="btn danger" onClick={() => act(() => monApi.clearHistory(props.serverId, props.dbId), t("History cleared"))}>{t("Clear")}</button>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <div className="muted" style={{ marginBottom: 10 }}>
          {tracking
            ? <>{t("Sampling every {interval} s, kept for {hours} h (change in Settings). Last sample: {last}.", { interval: data.settings.interval, hours: data.settings.retention_hours, last: tracking.last_sample ? new Date(tracking.last_sample * 1000).toLocaleString() : "–" })}</>
            : <>{t("History is not recorded for this database. Enable it to store a snapshot every {interval} s in fbadmin's local SQLite database.", { interval: data.settings.interval })}</>}
          {tracking?.last_error && <div className="err">{t("Last error: {error}", { error: tracking.last_error })}</div>}
          {data.settings.interval === 0 && <div className="warn">{t("The background sampler is disabled (interval 0).")}</div>}
          {" "}{t("{n} samples in range.", { n: data.samples.length })}
        </div>
      )}
      <ChartGrid points={points} loading={loading} longRange={hours > 24} />
      <SlowStatements {...props} />
    </>
  );
}

function SlowStatements(props: Props) {
  const app = useApp();
  const [rows, setRows] = useState<Record<string, any>[]>([]);
  useEffect(() => { monApi.slow(props.serverId, props.dbId).then((r) => setRows(r.statements)).catch(() => {}); }, []);
  if (!rows.length) return null;
  return (
    <>
      <h3>{t("Slow statements seen by the sampler")}</h3>
      <div className="grid-wrap">
        <table className="grid">
          <thead><tr><th>{t("Max duration")}</th><th>{t("Seen")}</th><th>{t("Last seen")}</th><th>{t("SQL")}</th><th /></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="num">{t("{n} s", { n: r.max_seconds?.toFixed(1) ?? "" })}</td><td className="num">{r.seen}</td>
                <td>{new Date(r.last_seen * 1000).toLocaleString()}</td>
                <td className="mono-cell" title={r.sql_text}>{r.sql_text}</td>
                <td><button className="btn small" onClick={() => app.openQueryTool(props.serverId, props.dbId, r.sql_text)}><IconTerminal size={12} /> {t("Open")}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ table statistics
const TABLE_COLS: [string, string][] = [
  ["reads", N_("Reads")], ["seq_reads", N_("Seq reads")], ["idx_reads", N_("Idx reads")], ["inserts", N_("Inserts")],
  ["updates", N_("Updates")], ["deletes", N_("Deletes")], ["backouts", N_("Backouts")], ["purges", N_("Purges")],
  ["expunges", N_("Expunges")], ["waits", N_("Lock waits")], ["conflicts", N_("Conflicts")], ["backversion_reads", N_("Back-version reads")],
];

function TablesPanel(props: Props) {
  const [order, setOrder] = useState("reads");
  const [system, setSystem] = useState(false);
  const [data, setData] = useState<{ available: boolean; reason?: string; tables: Record<string, any>[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => monApi.tables(props.serverId, props.dbId, { order_by: order, include_system: system, limit: 100 })
    .then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, [order, system]);
  const max = Math.max(1, ...(data?.tables ?? []).map((t) => Number(t[order]) || 0));
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <label>{t("Top tables by")}
          <select className="text" value={order} onChange={(e) => setOrder(e.target.value)}>
            {TABLE_COLS.map(([k, l]) => <option key={k} value={k}>{t(l)}</option>)}
            <option value="writes">{t("Writes (ins+upd+del)")}</option>
          </select>
        </label>
        <label className="check"><input type="checkbox" checked={system} onChange={(e) => setSystem(e.target.checked)} /> {t("System tables")}</label>
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        <span className="muted">{t("MON$TABLE_STATS, cumulative since the database was opened.")}</span>
        {error && <span className="err">{error}</span>}
      </div>
      {data && !data.available && <div className="muted">{data.reason}</div>}
      {data?.available && (data.tables.length === 0 ? <div className="muted">{t("No table activity recorded yet.")}</div> : (
        <div className="grid-wrap">
          <table className="grid">
            <thead><tr><th>{t("Table")}</th>{TABLE_COLS.map(([k, l]) => <th key={k} style={k === order ? { color: "var(--accent)" } : undefined}>{t(l)}</th>)}</tr></thead>
            <tbody>
              {data.tables.map((t) => (
                <tr key={t.table_name}>
                  <td>{t.table_name}</td>
                  {TABLE_COLS.map(([k]) => k === order ? (
                    <td key={k} className="num bar-cell"><div className="bar" style={{ width: `${(100 * (Number(t[k]) || 0)) / max}%` }} /><span className="v">{fmtNum(t[k])}</span></td>
                  ) : <td key={k} className="num">{fmtNum(t[k])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}

// ------------------------------------------------------------------ transactions / OAT
const ISO_SHORT: Record<number, string> = { 0: N_("consistency"), 1: N_("snapshot"), 2: N_("read committed"), 3: N_("rc no rec ver"), 4: N_("read consistency") };

function Alerts(props: { alerts: { level: string; message: string }[]; okText?: string }) {
  if (!props.alerts.length) return props.okText ? <div className="alerts"><div className="alert good"><span className="lvl">{t("OK")}</span><span>{props.okText}</span></div></div> : null;
  return (
    <div className="alerts">
      {props.alerts.map((a, i) => <div key={i} className={"alert " + a.level}><span className="lvl">{a.level}</span><span>{a.message}</span></div>)}
    </div>
  );
}

function TransactionsPanel(props: Props) {
  const app = useApp();
  const [longRunning, setLongRunning] = useState(300);
  const [gapWarn, setGapWarn] = useState(10000);
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => monApi.transactions(props.serverId, props.dbId, { long_running: longRunning, gap_warn: gapWarn })
    .then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, [longRunning, gapWarn]);
  useEffect(() => {
    if (!props.visible) return;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [props.visible, longRunning, gapWarn]);
  const kill = async (id: number) => {
    try { await api.killAttachment(props.serverId, props.dbId, id); app.toast(t("Attachment {id} disconnected", { id })); load(); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <label>{t("Long-running after")} <input className="text" type="number" min={1} style={{ width: 80 }} value={longRunning} onChange={(e) => setLongRunning(Math.max(1, Number(e.target.value)))} /> {t("s")}</label>
        <label>{t("Warn when a gap exceeds")} <input className="text" type="number" min={1} style={{ width: 100 }} value={gapWarn} onChange={(e) => setGapWarn(Math.max(1, Number(e.target.value)))} /></label>
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <>
          <Alerts alerts={data.alerts} okText={t("No long-running transactions and the transaction gaps are within limits.")} />
          <div className="cards">
            <div className="card"><div className="k">{t("Next transaction")}</div><div className="v">{fmtNum(data.database.next_transaction)}</div></div>
            <div className="card"><div className="k">{t("OAT (oldest active)")}</div><div className="v">{fmtNum(data.database.oat)}</div></div>
            <div className="card"><div className="k">{t("OIT (oldest interesting)")}</div><div className="v">{fmtNum(data.database.oit)}</div></div>
            <div className="card"><div className="k">{t("OST (oldest snapshot)")}</div><div className="v">{fmtNum(data.database.ost)}</div></div>
            <div className="card"><div className="k">{t("Next − OAT")}</div><div className="v">{fmtNum(data.gaps.next_oat)}</div></div>
            <div className="card"><div className="k">OAT − OIT</div><div className="v">{fmtNum(data.gaps.oat_oit)}</div></div>
          </div>
          <h3>{t("Who is holding the OAT")}</h3>
          {data.oat_holders.length === 0 ? <div className="muted">{t("No live transaction holds the OAT.")}</div> : data.oat_holders.map((tx: any) => (
            <div key={tx.id} className="sugg">
              <div className="h">
                <b>{t("Transaction {id}", { id: tx.id })}</b>
                <span className="muted">{t("· attachment {id} · {user}@{host} · {process} (pid {pid})", { id: tx.attachment_id, user: tx.username, host: tx.remote_address || t("local"), process: (tx.remote_process || "?").split(/[\\/]/).pop() ?? "", pid: tx.remote_pid ?? "?" })}</span>
                <span style={{ flex: 1 }} />
                {!tx.is_self && <button className="btn small danger" onClick={() => kill(tx.attachment_id)}>{t("Disconnect attachment")}</button>}
              </div>
              <div className="muted" style={{ marginTop: 4 }}>
                {t("Started {time} ({age} ago) · {isolation}{readonly} · {n} record changes", { time: fmtTs(tx.started_at), age: fmtAge(tx.age_seconds), isolation: ISO_SHORT[tx.isolation] ? t(ISO_SHORT[tx.isolation]) : tx.isolation, readonly: tx.read_only ? " " + t("read only") : "", n: tx.writes })}
                {tx.is_self ? " · " + t("this is fbadmin's own metadata connection") : ""}
              </div>
              {tx.statements.length > 0 && tx.statements.map((s: any) => (
                <div key={s.id} className="code-block">{s.state === 1 ? t("▶ running: ") : s.state === 2 ? t("⏸ open cursor: ") : t("idle: ")}{s.sql_text}</div>
              ))}
              {tx.statements.length === 0 && <div className="muted" style={{ marginTop: 4 }}>{t("No statement is running in it: the application left the transaction open (idle in transaction).")}</div>}
            </div>
          ))}
          <h3>{t("Transactions ({n})", { n: data.transactions.length })}</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>{t("ID")}</th><th>{t("Age")}</th><th>{t("Attachment")}</th><th>{t("User")}</th><th>{t("Process")}</th><th>{t("Isolation")}</th><th>{t("Changes")}</th><th>{t("Lock timeout")}</th><th>{t("Statement")}</th></tr></thead>
              <tbody>
                {data.transactions.map((tx: any) => (
                  <tr key={tx.id} style={tx.age_seconds >= longRunning && !tx.is_self ? { color: "var(--warn)" } : undefined}>
                    <td>{tx.id}{tx.id === data.database.oat ? " (OAT)" : ""}</td><td className="num">{fmtAge(tx.age_seconds)}</td>
                    <td>{tx.attachment_id}{tx.is_self ? " (fbadmin)" : ""}</td><td>{tx.username}</td>
                    <td title={tx.remote_process}>{(tx.remote_process ?? "").split(/[\\/]/).pop()}</td>
                    <td>{ISO_SHORT[tx.isolation] ? t(ISO_SHORT[tx.isolation]) : tx.isolation}{tx.read_only ? ", ro" : ""}</td><td className="num">{tx.writes}</td>
                    <td>{tx.lock_timeout === -1 ? t("wait") : tx.lock_timeout === 0 ? t("no wait") : t("{n} s", { n: tx.lock_timeout })}</td>
                    <td className="mono-cell" title={tx.statements.map((s: any) => s.sql_text).join("\n")}>{tx.statements[0]?.sql_text ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------------ locks
function LocksPanel(props: Props) {
  const app = useApp();
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => monApi.locks(props.serverId, props.dbId).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!props.visible) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [props.visible]);
  const cancel = async (id: number) => {
    try { await api.cancelStatement(props.serverId, props.dbId, id); app.toast(t("Statement {id} cancelled", { id })); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const kill = async (id: number) => {
    try { await api.killAttachment(props.serverId, props.dbId, id); app.toast(t("Attachment {id} disconnected", { id })); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        <span className="muted">{t("Auto-refresh every 3 s. Lock waits are inferred from MON$ record wait counters; the lock table itself (fb_lock_print) is not readable remotely.")}</span>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <>
          <h3>{t("Waiting for a record lock ({n})", { n: data.waiting.length })}</h3>
          {data.waiting.length === 0 ? <div className="alerts"><div className="alert good"><span className="lvl">{t("OK")}</span><span>{t("No statement is waiting for a lock.")}</span></div></div> : data.waiting.map((w: any) => (
            <div key={w.id} className="sugg">
              <div className="h">
                <b>{t("Statement {id}", { id: w.id })}</b>
                <span className="muted">{t("· attachment {id} · {user} · transaction {tx} · waiting {age} · lock timeout {timeout}", { id: w.attachment_id, user: w.username, tx: w.transaction_id, age: fmtAge(w.age_seconds), timeout: w.lock_timeout === -1 ? t("infinite (WAIT)") : w.lock_timeout === 0 ? "NO WAIT" : t("{n} s", { n: w.lock_timeout }) })}</span>
                <span style={{ flex: 1 }} />
                <button className="btn small danger" onClick={() => cancel(w.id)}>{t("Cancel statement")}</button>
              </div>
              <div className="code-block">{w.sql_text}</div>
              <div>{t("Tables:")} {w.tables.length ? w.tables.join(", ") : <span className="muted">{t("unknown (needs MON$TABLE_STATS)")}</span>}</div>
              {w.blockers.length > 0 ? (
                <div style={{ marginTop: 4 }}>{t("Likely blocked by:")}
                  {w.blockers.map((b: any) => (
                    <div key={b.id} style={{ marginLeft: 12 }}>
                      {t("transaction")} <b>{b.id}</b> {t("· attachment {id} · {user}@{host} · {process} · changed {tables} since {time}", { id: b.attachment_id, user: b.username, host: b.remote_address || t("local"), process: (b.remote_process || "?").split(/[\\/]/).pop() ?? "", tables: b.tables.join(", "), time: fmtTs(b.started_at) })}
                      {" "}<button className="btn small danger" onClick={() => kill(b.attachment_id)}>{t("Disconnect")}</button>
                    </div>
                  ))}
                </div>
              ) : <div className="muted">{t("No candidate blocker found (the holder may have committed already or changed the record through another table).")}</div>}
            </div>
          ))}
          {data.conflicts.length > 0 && (
            <>
              <h3>{t("Statements with update conflicts")}</h3>
              <ul>{data.conflicts.map((s: any) => <li key={s.id}>{t("#{id} ({n} conflicts):", { id: s.id, n: s.conflicts })} <span className="mono">{s.sql_text}</span></li>)}</ul>
            </>
          )}
          <h3>{t("Running statements ({n})", { n: data.running.length })}</h3>
          {data.running.length === 0 ? <div className="muted">{t("None (other than fbadmin's own).")}</div> : (
            <div className="grid-wrap">
              <table className="grid">
                <thead><tr><th>{t("ID")}</th><th>{t("Attachment")}</th><th>{t("Transaction")}</th><th>{t("State")}</th><th>{t("Duration")}</th><th>{t("Lock waits")}</th><th>{t("SQL")}</th></tr></thead>
                <tbody>
                  {data.running.map((s: any) => (
                    <tr key={s.id}><td>{s.id}</td><td>{s.attachment_id}</td><td>{s.transaction_id}</td><td>{s.state === 1 ? t("active") : t("stalled")}</td>
                      <td className="num">{fmtAge(s.age_seconds)}</td><td className="num">{s.waits}</td><td className="mono-cell" title={s.sql_text}>{s.sql_text}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data.config.length > 0 && (
            <>
              <h3>{t("Lock manager configuration (RDB$CONFIG)")}</h3>
              <table className="kv"><tbody>
                {data.config.map((c: any) => <tr key={c.name}><td>{c.name}</td><td>{c.config_value ?? "–"} {c.is_set ? "" : <span className="muted">{t("(default)")}</span>}</td></tr>)}
              </tbody></table>
            </>
          )}
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------------ compiled statement cache
function CompiledPanel(props: Props) {
  const app = useApp();
  const [search, setSearch] = useState("");
  const [data, setData] = useState<{ available: boolean; reason?: string; statements: Record<string, any>[] } | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => monApi.compiled(props.serverId, props.dbId, search || undefined).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <input className="text" placeholder={t("Search SQL or object…")} value={search} onChange={(e) => setSearch(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        <span className="muted">{t("MON$COMPILED_STATEMENTS: statements and PSQL routines in the compiled statement cache.")}</span>
        {error && <span className="err">{error}</span>}
      </div>
      {data && !data.available && <div className="muted">{data.reason}</div>}
      {data?.available && (
        <div className="grid-wrap">
          <table className="grid">
            <thead><tr><th>{t("ID")}</th><th>{t("Object")}</th><th>{t("Type")}</th><th>{t("In use")}</th><th>{t("Running")}</th><th>{t("Page reads")}</th><th>{t("Fetches")}</th><th>{t("Seq reads")}</th><th>{t("Idx reads")}</th><th>{t("Memory")}</th><th>{t("SQL / plan")}</th></tr></thead>
            <tbody>
              {data.statements.map((s) => (
                <tr key={s.id} onClick={() => setOpen(open === s.id ? null : s.id)} style={{ cursor: "pointer" }}>
                  <td>{s.id}</td><td>{s.package_name ? `${s.package_name}.` : ""}{s.object_name ?? ""}</td><td>{s.object_type_name ?? (s.object_name ? "" : "SQL")}</td>
                  <td className="num">{s.statements}</td><td className="num">{s.active}</td>
                  <td className="num">{fmtNum(s.page_reads)}</td><td className="num">{fmtNum(s.page_fetches)}</td>
                  <td className="num">{fmtNum(s.seq_reads)}</td><td className="num">{fmtNum(s.idx_reads)}</td><td className="num">{fmtBytes(s.memory_used)}</td>
                  <td className="mono-cell" style={open === s.id ? { whiteSpace: "pre-wrap", maxWidth: 700 } : undefined}>
                    {open === s.id ? <>{s.sql_text}{"\n\n"}{s.plan}{s.sql_text && <div><button className="btn small" onClick={(e) => { e.stopPropagation(); app.openQueryTool(props.serverId, props.dbId, s.sql_text); }}><IconTerminal size={12} /> {t("Open in Query Tool")}</button></div>}</> : (s.sql_text ?? "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------------------ index advisor
function AdvisorPanel(props: Props) {
  const app = useApp();
  const [sources, setSources] = useState<string[]>(["running", "history", "compiled"]);
  const [sql, setSql] = useState("");
  const [minRows, setMinRows] = useState(1000);
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const toggle = (s: string) => setSources((v) => (v.includes(s) ? v.filter((x) => x !== s) : [...v, s]));
  const run = async () => {
    setBusy(true);
    try { setData(await monApi.advisor(props.serverId, props.dbId, { sources, sql, min_rows: minRows })); setError(null); }
    catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  };
  const copy = (text: string) => { navigator.clipboard?.writeText(text); app.toast(t("Copied")); };
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        {t("Finds full table scans (NATURAL) of large tables in statement plans and suggests indices for the filtered and joined columns. Workload: statements running now (MON$STATEMENTS), slow statements recorded by the history sampler, the compiled statement cache (Firebird 5) and/or SQL pasted below.")}
      </p>
      <div className="toolbar" style={{ margin: "0 -16px 8px" }}>
        {[["running", t("Running statements")], ["history", t("Slow statements (history)")], ["compiled", t("Statement cache (FB5)")]].map(([k, l]) => (
          <label key={k} className="check"><input type="checkbox" checked={sources.includes(k)} onChange={() => toggle(k)} /> {l}</label>
        ))}
        <label>{t("Min. rows")} <input className="text" type="number" style={{ width: 90 }} value={minRows} onChange={(e) => setMinRows(Number(e.target.value))} /></label>
        <button className="btn primary" disabled={busy} onClick={run}><IconPlay /> {t("Analyze")}</button>
        {error && <span className="err">{error}</span>}
      </div>
      <div style={{ height: 140, border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
        <SqlEditor value={sql} onChange={setSql} onExecute={run} placeholder={t("Paste a workload (statements separated by ;) — optional")} />
      </div>
      {data && (
        <>
          <h3>{t("Suggestions ({n})", { n: data.suggestions.length })}</h3>
          {data.suggestions.length === 0 && <div className="muted">{t("No index suggestions for the analyzed statements.")}</div>}
          {data.suggestions.map((s: any) => (
            <div key={s.ddl} className="sugg">
              <div className="h">
                <b>{s.table}</b><span className="muted">{t("({columns}) · {rows} · seen {n}×{total}", { columns: s.columns.join(", "), rows: s.rows === null ? t("rows unknown") : t("{n} rows", { n: fmtNum(s.rows) }), n: s.occurrences, total: s.total_seconds ? " · " + t("{n} s total", { n: s.total_seconds.toFixed(1) }) : "" })}</span>
                <span style={{ flex: 1 }} />
                <button className="btn small" onClick={() => copy(s.ddl)}><IconCopy size={12} /> {t("Copy")}</button>
                <button className="btn small" onClick={() => app.openQueryTool(props.serverId, props.dbId, s.ddl)}><IconTerminal size={12} /> {t("Open in Query Tool")}</button>
              </div>
              <div className="code-block">{s.ddl}</div>
              <div>{s.reason}</div>
              <details style={{ marginTop: 4 }}><summary className="muted">{t("Statements")}</summary>{s.statements.map((q: string, i: number) => <div key={i} className="code-block">{q}</div>)}</details>
            </div>
          ))}
          {data.notes.length > 0 && (
            <>
              <h3>{t("Notes")}</h3>
              <div className="alerts">{data.notes.map((n: any, i: number) => <div key={i} className="alert warning"><span className="lvl">{t("note")}</span><span>{n.message}</span></div>)}</div>
            </>
          )}
          <h3>{t("Analyzed statements ({n})", { n: data.statements.length })}</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>{t("Full scans")}</th><th>{t("Suggestions")}</th><th>{t("SQL")}</th><th>{t("Plan")}</th></tr></thead>
              <tbody>
                {data.statements.map((s: any, i: number) => (
                  <tr key={i}>
                    <td>{s.full_scans.join(", ") || <span className="muted">{t("none")}</span>}</td><td className="num">{s.suggestions.length}</td>
                    <td className="mono-cell" title={s.sql}>{s.sql}</td>
                    <td className="mono-cell" title={s.plan ?? s.error}>{s.error ? <span className="err">{s.error}</span> : (s.plan ?? "").split("\n").join(" ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------------ settings
function SettingsPanel() {
  const app = useApp();
  const [s, setS] = useState<MonSettings | null>(null);
  const [tracked, setTracked] = useState<any[]>([]);
  useEffect(() => { monApi.settings().then((r) => { setS(r.settings); setTracked(r.tracked); }).catch((e) => app.toast(errMsg(e), "error")); }, []);
  if (!s) return null;
  const save = async () => {
    try { const r = await monApi.saveSettings(s); setS(r.settings); app.toast(t("Settings saved")); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const name = (sid: string, dbid: string) => {
    const srv = app.servers.find((x) => x.id === sid);
    return `${srv?.name ?? sid} / ${srv?.databases.find((d) => d.id === dbid)?.name ?? dbid}`;
  };
  return (
    <>
      <h3 style={{ marginTop: 0 }}>{t("History sampler")}</h3>
      <div className="form-grid">
        <label>{t("Sample interval (seconds, 0 = off)")}</label><input className="text" type="number" min={0} value={s.interval} onChange={(e) => setS({ ...s, interval: Number(e.target.value) })} />
        <label>{t("Retention (hours)")}</label><input className="text" type="number" min={1} value={s.retention_hours} onChange={(e) => setS({ ...s, retention_hours: Number(e.target.value) })} />
        <label>{t("Log statements running longer than (s, 0 = off)")}</label><input className="text" type="number" min={0} value={s.slow_seconds} onChange={(e) => setS({ ...s, slow_seconds: Number(e.target.value) })} />
        <label>{t("Prometheus exporter")}</label>
        <label className="check" style={{ color: "var(--text)" }}><input type="checkbox" checked={s.metrics_enabled} onChange={(e) => setS({ ...s, metrics_enabled: e.target.checked })} /> {t("Serve")} <code>/metrics</code> {t("for databases with history enabled")} (<code>/metrics?all=1</code> {t("for every registered database")})</label>
      </div>
      <div style={{ marginTop: 10 }}><button className="btn primary" onClick={save}>{t("Save")}</button></div>
      <p className="muted">{t("Samples are stored in")} <code>monitoring.sqlite</code> {t("in fbadmin's data directory. Environment defaults: {vars}.", { vars: "FBADMIN_HISTORY_INTERVAL, FBADMIN_HISTORY_RETENTION_HOURS, FBADMIN_METRICS, FBADMIN_METRICS_TOKEN" })}</p>
      <h3>{t("Databases with history enabled")}</h3>
      {tracked.length === 0 ? <div className="muted">{t("None.")}</div> : (
        <table className="kv"><tbody>
          {tracked.map((tr) => <tr key={tr.server_id + tr.db_id}><td>{name(tr.server_id, tr.db_id)}</td><td>{tr.last_sample ? t("last sample {time}", { time: new Date(tr.last_sample * 1000).toLocaleString() }) : t("no sample yet")}{tr.last_error && <span className="err"> · {tr.last_error}</span>}</td></tr>)}
        </tbody></table>
      )}
    </>
  );
}

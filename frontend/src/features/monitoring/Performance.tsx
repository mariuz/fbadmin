import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import { IconCopy, IconPlay, IconRefresh, IconTerminal } from "../../components/Icons";
import { SqlEditor } from "../../components/SqlEditor";
import { LineChart, type Series } from "./LineChart";
import { fmtAge, fmtBytes, fmtNum, fmtTs, monApi, type Features, type MonSettings, type Sample, type Snapshot } from "./api";

type Props = { serverId: string; dbId: string; visible: boolean };

const SUBTABS = [
  ["live", "Live"],
  ["history", "History"],
  ["tables", "Table statistics"],
  ["transactions", "Transactions & OAT"],
  ["locks", "Locks"],
  ["compiled", "Statement cache"],
  ["advisor", "Index advisor"],
  ["settings", "Settings"],
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
          <button key={k} className={sub === k ? "active" : ""} onClick={() => setSub(k)}>{label}</button>
        ))}
        <span style={{ flex: 1 }} />
        {features && <span className="muted" style={{ alignSelf: "center", fontSize: 12 }}>Firebird {features.engine_version}</span>}
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
      <LineChart {...common} title="Attachments" series={[s("att", "Attachments", 1, g("attachments"))]} format={(v) => fmtNum(v)} />
      <LineChart {...common} title="Transactions" unit="per second" series={[s("tps", "Transactions/s", 1, r("next_transaction"))]} format={perSec} />
      <LineChart {...common} title="Page I/O" unit="pages per second" series={[s("pr", "Reads", 1, r("page_reads")), s("pw", "Writes", 2, r("page_writes"))]} format={perSec} />
      <LineChart {...common} title="Page cache" unit="per second" series={[s("pf", "Fetches", 1, r("page_fetches")), s("pm", "Marks", 2, r("page_marks"))]} format={perSec} />
      <LineChart {...common} title="Record reads" unit="records per second" series={[s("sr", "Sequential (full scan)", 1, r("seq_reads")), s("ir", "Indexed", 2, r("idx_reads"))]} format={perSec} />
      <LineChart {...common} title="Record changes" unit="records per second" series={[s("ins", "Inserts", 1, r("inserts")), s("upd", "Updates", 2, r("updates")), s("del", "Deletes", 3, r("deletes"))]} format={perSec} />
      <LineChart {...common} title="Garbage" unit="records per second" series={[s("bo", "Backouts", 1, r("backouts")), s("pu", "Purges", 2, r("purges")), s("ex", "Expunges", 3, r("expunges"))]} format={perSec} />
      <LineChart {...common} binary title="Memory" series={[s("mu", "Used", 1, g("memory_used")), s("ma", "Allocated", 2, g("memory_allocated"))]} format={(v) => fmtBytes(v)} />
      <LineChart {...common} title="Activity" series={[s("tx", "Open transactions", 1, g("transactions")), s("st", "Running statements", 2, g("active_statements"))]} format={(v) => fmtNum(v)} />
      <LineChart {...common} title="Next transaction − OAT" unit="transactions" series={[s("gap", "Gap", 1, gap)]} format={(v) => fmtNum(v)} />
      <LineChart {...common} title="Lock contention" unit="per second" series={[s("rw", "Record waits", 1, r("record_waits")), s("rc", "Update conflicts", 2, r("record_conflicts"))]} format={perSec} />
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
      <div className="card"><div className="k">Attachments</div><div className="v">{fmtNum(p.snap.attachments)}</div></div>
      <div className="card"><div className="k">Transactions / s</div><div className="v">{perSec(p.rates.next_transaction)}</div></div>
      <div className="card"><div className="k">Cache hit ratio</div><div className="v">{hit === null ? "–" : hit.toFixed(1) + " %"}</div></div>
      <div className="card"><div className="k">Next − OAT</div><div className="v">{fmtNum(gap)}</div></div>
      <div className="card"><div className="k">Memory used</div><div className="v">{fmtBytes(p.snap.memory_used)}</div></div>
      <div className="card"><div className="k">Running statements</div><div className="v">{fmtNum(p.snap.active_statements)}</div></div>
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
        <label>Refresh every
          <select className="text" value={interval} onChange={(e) => setIntervalSec(Number(e.target.value))}>
            {[1, 2, 5, 10, 30].map((s) => <option key={s} value={s}>{s} s</option>)}
          </select>
        </label>
        <button className={"btn" + (paused ? " on" : "")} onClick={() => setPaused((v) => !v)}>{paused ? "Resume" : "Pause"}</button>
        <button className="btn" onClick={() => { prev.current = null; setPoints([]); poll(); }}>Reset</button>
        <span className="muted">Database-wide MON$ counters since the database was opened; rates are deltas between samples.</span>
        {error && <span className="err">{error}</span>}
      </div>
      <Tiles last={points[points.length - 1]} />
      <ChartGrid points={points} />
    </>
  );
}

// ------------------------------------------------------------------ history
const RANGES: [number, string][] = [[1, "1 h"], [6, "6 h"], [24, "24 h"], [72, "3 d"], [168, "7 d"]];

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
            e.target.checked ? "History recording enabled" : "History recording disabled")} />
          Record history for this database
        </label>
        <span className="sep" />
        <div className="seg">
          {RANGES.map(([h, l]) => <button key={h} className={hours === h ? "on" : ""} onClick={() => setHours(h)}>{l}</button>)}
        </div>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        <button className="btn" disabled={!tracking} onClick={() => act(() => monApi.sampleNow(props.serverId, props.dbId))}>Sample now</button>
        <button className="btn danger" onClick={() => act(() => monApi.clearHistory(props.serverId, props.dbId), "History cleared")}>Clear</button>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <div className="muted" style={{ marginBottom: 10 }}>
          {tracking
            ? <>Sampling every {data.settings.interval} s, kept for {data.settings.retention_hours} h (change in Settings). Last sample: {tracking.last_sample ? new Date(tracking.last_sample * 1000).toLocaleString() : "–"}.</>
            : <>History is not recorded for this database. Enable it to store a snapshot every {data.settings.interval} s in fbadmin's local SQLite database.</>}
          {tracking?.last_error && <div className="err">Last error: {tracking.last_error}</div>}
          {data.settings.interval === 0 && <div className="warn">The background sampler is disabled (interval 0).</div>}
          {" "}{data.samples.length} samples in range.
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
      <h3>Slow statements seen by the sampler</h3>
      <div className="grid-wrap">
        <table className="grid">
          <thead><tr><th>Max duration</th><th>Seen</th><th>Last seen</th><th>SQL</th><th /></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="num">{r.max_seconds?.toFixed(1)} s</td><td className="num">{r.seen}</td>
                <td>{new Date(r.last_seen * 1000).toLocaleString()}</td>
                <td className="mono-cell" title={r.sql_text}>{r.sql_text}</td>
                <td><button className="btn small" onClick={() => app.openQueryTool(props.serverId, props.dbId, r.sql_text)}><IconTerminal size={12} /> Open</button></td>
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
  ["reads", "Reads"], ["seq_reads", "Seq reads"], ["idx_reads", "Idx reads"], ["inserts", "Inserts"],
  ["updates", "Updates"], ["deletes", "Deletes"], ["backouts", "Backouts"], ["purges", "Purges"],
  ["expunges", "Expunges"], ["waits", "Lock waits"], ["conflicts", "Conflicts"], ["backversion_reads", "Back-version reads"],
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
        <label>Top tables by
          <select className="text" value={order} onChange={(e) => setOrder(e.target.value)}>
            {TABLE_COLS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            <option value="writes">Writes (ins+upd+del)</option>
          </select>
        </label>
        <label className="check"><input type="checkbox" checked={system} onChange={(e) => setSystem(e.target.checked)} /> System tables</label>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        <span className="muted">MON$TABLE_STATS, cumulative since the database was opened.</span>
        {error && <span className="err">{error}</span>}
      </div>
      {data && !data.available && <div className="muted">{data.reason}</div>}
      {data?.available && (data.tables.length === 0 ? <div className="muted">No table activity recorded yet.</div> : (
        <div className="grid-wrap">
          <table className="grid">
            <thead><tr><th>Table</th>{TABLE_COLS.map(([k, l]) => <th key={k} style={k === order ? { color: "var(--accent)" } : undefined}>{l}</th>)}</tr></thead>
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
const ISO_SHORT: Record<number, string> = { 0: "consistency", 1: "snapshot", 2: "read committed", 3: "rc no rec ver", 4: "read consistency" };

function Alerts(props: { alerts: { level: string; message: string }[]; okText?: string }) {
  if (!props.alerts.length) return props.okText ? <div className="alerts"><div className="alert good"><span className="lvl">OK</span><span>{props.okText}</span></div></div> : null;
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
    try { await api.killAttachment(props.serverId, props.dbId, id); app.toast(`Attachment ${id} disconnected`); load(); }
    catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <label>Long-running after <input className="text" type="number" min={1} style={{ width: 80 }} value={longRunning} onChange={(e) => setLongRunning(Math.max(1, Number(e.target.value)))} /> s</label>
        <label>Warn when a gap exceeds <input className="text" type="number" min={1} style={{ width: 100 }} value={gapWarn} onChange={(e) => setGapWarn(Math.max(1, Number(e.target.value)))} /></label>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <>
          <Alerts alerts={data.alerts} okText="No long-running transactions and the transaction gaps are within limits." />
          <div className="cards">
            <div className="card"><div className="k">Next transaction</div><div className="v">{fmtNum(data.database.next_transaction)}</div></div>
            <div className="card"><div className="k">OAT (oldest active)</div><div className="v">{fmtNum(data.database.oat)}</div></div>
            <div className="card"><div className="k">OIT (oldest interesting)</div><div className="v">{fmtNum(data.database.oit)}</div></div>
            <div className="card"><div className="k">OST (oldest snapshot)</div><div className="v">{fmtNum(data.database.ost)}</div></div>
            <div className="card"><div className="k">Next − OAT</div><div className="v">{fmtNum(data.gaps.next_oat)}</div></div>
            <div className="card"><div className="k">OAT − OIT</div><div className="v">{fmtNum(data.gaps.oat_oit)}</div></div>
          </div>
          <h3>Who is holding the OAT</h3>
          {data.oat_holders.length === 0 ? <div className="muted">No live transaction holds the OAT.</div> : data.oat_holders.map((t: any) => (
            <div key={t.id} className="sugg">
              <div className="h">
                <b>Transaction {t.id}</b>
                <span className="muted">· attachment {t.attachment_id} · {t.username}@{t.remote_address || "local"} · {(t.remote_process || "?").split(/[\\/]/).pop()} (pid {t.remote_pid ?? "?"})</span>
                <span style={{ flex: 1 }} />
                {!t.is_self && <button className="btn small danger" onClick={() => kill(t.attachment_id)}>Disconnect attachment</button>}
              </div>
              <div className="muted" style={{ marginTop: 4 }}>
                Started {fmtTs(t.started_at)} ({fmtAge(t.age_seconds)} ago) · {ISO_SHORT[t.isolation] ?? t.isolation}{t.read_only ? " read only" : ""} · {t.writes} record changes
                {t.is_self ? " · this is fbadmin's own metadata connection" : ""}
              </div>
              {t.statements.length > 0 && t.statements.map((s: any) => (
                <div key={s.id} className="code-block">{s.state === 1 ? "▶ running: " : s.state === 2 ? "⏸ open cursor: " : "idle: "}{s.sql_text}</div>
              ))}
              {t.statements.length === 0 && <div className="muted" style={{ marginTop: 4 }}>No statement is running in it: the application left the transaction open (idle in transaction).</div>}
            </div>
          ))}
          <h3>Transactions ({data.transactions.length})</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>ID</th><th>Age</th><th>Attachment</th><th>User</th><th>Process</th><th>Isolation</th><th>Changes</th><th>Lock timeout</th><th>Statement</th></tr></thead>
              <tbody>
                {data.transactions.map((t: any) => (
                  <tr key={t.id} style={t.age_seconds >= longRunning && !t.is_self ? { color: "var(--warn)" } : undefined}>
                    <td>{t.id}{t.id === data.database.oat ? " (OAT)" : ""}</td><td className="num">{fmtAge(t.age_seconds)}</td>
                    <td>{t.attachment_id}{t.is_self ? " (fbadmin)" : ""}</td><td>{t.username}</td>
                    <td title={t.remote_process}>{(t.remote_process ?? "").split(/[\\/]/).pop()}</td>
                    <td>{ISO_SHORT[t.isolation] ?? t.isolation}{t.read_only ? ", ro" : ""}</td><td className="num">{t.writes}</td>
                    <td>{t.lock_timeout === -1 ? "wait" : t.lock_timeout === 0 ? "no wait" : `${t.lock_timeout} s`}</td>
                    <td className="mono-cell" title={t.statements.map((s: any) => s.sql_text).join("\n")}>{t.statements[0]?.sql_text ?? ""}</td>
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
    try { await api.cancelStatement(props.serverId, props.dbId, id); app.toast(`Statement ${id} cancelled`); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const kill = async (id: number) => {
    try { await api.killAttachment(props.serverId, props.dbId, id); app.toast(`Attachment ${id} disconnected`); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  return (
    <>
      <div className="toolbar" style={{ margin: "-12px -16px 12px" }}>
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        <span className="muted">Auto-refresh every 3 s. Lock waits are inferred from MON$ record wait counters; the lock table itself (fb_lock_print) is not readable remotely.</span>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <>
          <h3>Waiting for a record lock ({data.waiting.length})</h3>
          {data.waiting.length === 0 ? <div className="alerts"><div className="alert good"><span className="lvl">OK</span><span>No statement is waiting for a lock.</span></div></div> : data.waiting.map((w: any) => (
            <div key={w.id} className="sugg">
              <div className="h">
                <b>Statement {w.id}</b>
                <span className="muted">· attachment {w.attachment_id} · {w.username} · transaction {w.transaction_id} · waiting {fmtAge(w.age_seconds)} · lock timeout {w.lock_timeout === -1 ? "infinite (WAIT)" : w.lock_timeout === 0 ? "NO WAIT" : `${w.lock_timeout} s`}</span>
                <span style={{ flex: 1 }} />
                <button className="btn small danger" onClick={() => cancel(w.id)}>Cancel statement</button>
              </div>
              <div className="code-block">{w.sql_text}</div>
              <div>Tables: {w.tables.length ? w.tables.join(", ") : <span className="muted">unknown (needs MON$TABLE_STATS)</span>}</div>
              {w.blockers.length > 0 ? (
                <div style={{ marginTop: 4 }}>Likely blocked by:
                  {w.blockers.map((b: any) => (
                    <div key={b.id} style={{ marginLeft: 12 }}>
                      transaction <b>{b.id}</b> · attachment {b.attachment_id} · {b.username}@{b.remote_address || "local"} · {(b.remote_process || "?").split(/[\\/]/).pop()} · changed {b.tables.join(", ")} since {fmtTs(b.started_at)}
                      {" "}<button className="btn small danger" onClick={() => kill(b.attachment_id)}>Disconnect</button>
                    </div>
                  ))}
                </div>
              ) : <div className="muted">No candidate blocker found (the holder may have committed already or changed the record through another table).</div>}
            </div>
          ))}
          {data.conflicts.length > 0 && (
            <>
              <h3>Statements with update conflicts</h3>
              <ul>{data.conflicts.map((s: any) => <li key={s.id}>#{s.id} ({s.conflicts} conflicts): <span className="mono">{s.sql_text}</span></li>)}</ul>
            </>
          )}
          <h3>Running statements ({data.running.length})</h3>
          {data.running.length === 0 ? <div className="muted">None (other than fbadmin's own).</div> : (
            <div className="grid-wrap">
              <table className="grid">
                <thead><tr><th>ID</th><th>Attachment</th><th>Transaction</th><th>State</th><th>Duration</th><th>Lock waits</th><th>SQL</th></tr></thead>
                <tbody>
                  {data.running.map((s: any) => (
                    <tr key={s.id}><td>{s.id}</td><td>{s.attachment_id}</td><td>{s.transaction_id}</td><td>{s.state === 1 ? "active" : "stalled"}</td>
                      <td className="num">{fmtAge(s.age_seconds)}</td><td className="num">{s.waits}</td><td className="mono-cell" title={s.sql_text}>{s.sql_text}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data.config.length > 0 && (
            <>
              <h3>Lock manager configuration (RDB$CONFIG)</h3>
              <table className="kv"><tbody>
                {data.config.map((c: any) => <tr key={c.name}><td>{c.name}</td><td>{c.config_value ?? "–"} {c.is_set ? "" : <span className="muted">(default)</span>}</td></tr>)}
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
        <input className="text" placeholder="Search SQL or object…" value={search} onChange={(e) => setSearch(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        <span className="muted">MON$COMPILED_STATEMENTS: statements and PSQL routines in the compiled statement cache.</span>
        {error && <span className="err">{error}</span>}
      </div>
      {data && !data.available && <div className="muted">{data.reason}</div>}
      {data?.available && (
        <div className="grid-wrap">
          <table className="grid">
            <thead><tr><th>ID</th><th>Object</th><th>Type</th><th>In use</th><th>Running</th><th>Page reads</th><th>Fetches</th><th>Seq reads</th><th>Idx reads</th><th>Memory</th><th>SQL / plan</th></tr></thead>
            <tbody>
              {data.statements.map((s) => (
                <tr key={s.id} onClick={() => setOpen(open === s.id ? null : s.id)} style={{ cursor: "pointer" }}>
                  <td>{s.id}</td><td>{s.package_name ? `${s.package_name}.` : ""}{s.object_name ?? ""}</td><td>{s.object_type_name ?? (s.object_name ? "" : "SQL")}</td>
                  <td className="num">{s.statements}</td><td className="num">{s.active}</td>
                  <td className="num">{fmtNum(s.page_reads)}</td><td className="num">{fmtNum(s.page_fetches)}</td>
                  <td className="num">{fmtNum(s.seq_reads)}</td><td className="num">{fmtNum(s.idx_reads)}</td><td className="num">{fmtBytes(s.memory_used)}</td>
                  <td className="mono-cell" style={open === s.id ? { whiteSpace: "pre-wrap", maxWidth: 700 } : undefined}>
                    {open === s.id ? <>{s.sql_text}{"\n\n"}{s.plan}{s.sql_text && <div><button className="btn small" onClick={(e) => { e.stopPropagation(); app.openQueryTool(props.serverId, props.dbId, s.sql_text); }}><IconTerminal size={12} /> Open in Query Tool</button></div>}</> : (s.sql_text ?? "")}
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
  const copy = (t: string) => { navigator.clipboard?.writeText(t); app.toast("Copied"); };
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        Finds full table scans (NATURAL) of large tables in statement plans and suggests indices for the filtered and joined columns.
        Workload: statements running now (MON$STATEMENTS), slow statements recorded by the history sampler, the compiled statement cache (Firebird 5) and/or SQL pasted below.
      </p>
      <div className="toolbar" style={{ margin: "0 -16px 8px" }}>
        {[["running", "Running statements"], ["history", "Slow statements (history)"], ["compiled", "Statement cache (FB5)"]].map(([k, l]) => (
          <label key={k} className="check"><input type="checkbox" checked={sources.includes(k)} onChange={() => toggle(k)} /> {l}</label>
        ))}
        <label>Min. rows <input className="text" type="number" style={{ width: 90 }} value={minRows} onChange={(e) => setMinRows(Number(e.target.value))} /></label>
        <button className="btn primary" disabled={busy} onClick={run}><IconPlay /> Analyze</button>
        {error && <span className="err">{error}</span>}
      </div>
      <div style={{ height: 140, border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
        <SqlEditor value={sql} onChange={setSql} onExecute={run} placeholder="Paste a workload (statements separated by ;) — optional" />
      </div>
      {data && (
        <>
          <h3>Suggestions ({data.suggestions.length})</h3>
          {data.suggestions.length === 0 && <div className="muted">No index suggestions for the analyzed statements.</div>}
          {data.suggestions.map((s: any) => (
            <div key={s.ddl} className="sugg">
              <div className="h">
                <b>{s.table}</b><span className="muted">({s.columns.join(", ")}) · {s.rows === null ? "rows unknown" : `${fmtNum(s.rows)} rows`} · seen {s.occurrences}×{s.total_seconds ? ` · ${s.total_seconds.toFixed(1)} s total` : ""}</span>
                <span style={{ flex: 1 }} />
                <button className="btn small" onClick={() => copy(s.ddl)}><IconCopy size={12} /> Copy</button>
                <button className="btn small" onClick={() => app.openQueryTool(props.serverId, props.dbId, s.ddl)}><IconTerminal size={12} /> Open in Query Tool</button>
              </div>
              <div className="code-block">{s.ddl}</div>
              <div>{s.reason}</div>
              <details style={{ marginTop: 4 }}><summary className="muted">Statements</summary>{s.statements.map((q: string, i: number) => <div key={i} className="code-block">{q}</div>)}</details>
            </div>
          ))}
          {data.notes.length > 0 && (
            <>
              <h3>Notes</h3>
              <div className="alerts">{data.notes.map((n: any, i: number) => <div key={i} className="alert warning"><span className="lvl">note</span><span>{n.message}</span></div>)}</div>
            </>
          )}
          <h3>Analyzed statements ({data.statements.length})</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>Full scans</th><th>Suggestions</th><th>SQL</th><th>Plan</th></tr></thead>
              <tbody>
                {data.statements.map((s: any, i: number) => (
                  <tr key={i}>
                    <td>{s.full_scans.join(", ") || <span className="muted">none</span>}</td><td className="num">{s.suggestions.length}</td>
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
    try { const r = await monApi.saveSettings(s); setS(r.settings); app.toast("Settings saved"); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const name = (sid: string, dbid: string) => {
    const srv = app.servers.find((x) => x.id === sid);
    return `${srv?.name ?? sid} / ${srv?.databases.find((d) => d.id === dbid)?.name ?? dbid}`;
  };
  return (
    <>
      <h3 style={{ marginTop: 0 }}>History sampler</h3>
      <div className="form-grid">
        <label>Sample interval (seconds, 0 = off)</label><input className="text" type="number" min={0} value={s.interval} onChange={(e) => setS({ ...s, interval: Number(e.target.value) })} />
        <label>Retention (hours)</label><input className="text" type="number" min={1} value={s.retention_hours} onChange={(e) => setS({ ...s, retention_hours: Number(e.target.value) })} />
        <label>Log statements running longer than (s, 0 = off)</label><input className="text" type="number" min={0} value={s.slow_seconds} onChange={(e) => setS({ ...s, slow_seconds: Number(e.target.value) })} />
        <label>Prometheus exporter</label>
        <label className="check" style={{ color: "var(--text)" }}><input type="checkbox" checked={s.metrics_enabled} onChange={(e) => setS({ ...s, metrics_enabled: e.target.checked })} /> Serve <code>/metrics</code> for databases with history enabled (<code>/metrics?all=1</code> for every registered database)</label>
      </div>
      <div style={{ marginTop: 10 }}><button className="btn primary" onClick={save}>Save</button></div>
      <p className="muted">Samples are stored in <code>monitoring.sqlite</code> in fbadmin's data directory. Environment defaults: FBADMIN_HISTORY_INTERVAL, FBADMIN_HISTORY_RETENTION_HOURS, FBADMIN_METRICS, FBADMIN_METRICS_TOKEN.</p>
      <h3>Databases with history enabled</h3>
      {tracked.length === 0 ? <div className="muted">None.</div> : (
        <table className="kv"><tbody>
          {tracked.map((t) => <tr key={t.server_id + t.db_id}><td>{name(t.server_id, t.db_id)}</td><td>{t.last_sample ? `last sample ${new Date(t.last_sample * 1000).toLocaleString()}` : "no sample yet"}{t.last_error && <span className="err"> · {t.last_error}</span>}</td></tr>)}
        </tbody></table>
      )}
    </>
  );
}

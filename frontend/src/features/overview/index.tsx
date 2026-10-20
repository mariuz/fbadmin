/**
 * Milestone 2.0 — multi-server overview: one page with the health of every registered server and
 * database (reachability, version, attachments, transactions and the OAT gap, size, last backup,
 * replication, firing alert rules and failed scheduled jobs). The backend checks everything
 * concurrently with a timeout and never asks for passwords that are not saved.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import "./overview.css";
import { registerTabKind, registerTopbar } from "../../registry";
import { errMsg, useApp, type AppApi } from "../../context";
import { N_, t } from "../../i18n";
import { IconActivity, IconRefresh } from "../../components/Icons";
import { fmtAge, fmtBytes } from "../monitoring/api";
import { openPerformance } from "../monitoring";
import { oapi, type Issue, type Level, type Overview, type OverviewDb, type OverviewServer } from "./api";

const LEVELS: Level[] = ["critical", "warning", "unknown", "ok"];
const RANK: Record<Level, number> = { critical: 3, warning: 2, unknown: 1, ok: 0 };
const LEVEL_LABEL: Record<Level, string> = { ok: N_("OK"), warning: N_("Warning"), critical: N_("Critical"), unknown: N_("Unknown") };
const LEVEL_ICON: Record<Level, string> = { ok: "✓", warning: "!", critical: "✕", unknown: "?" };
const REFRESH: Array<[number, string]> = [[0, N_("Off")], [30, N_("30 s")], [60, N_("1 min")], [300, N_("5 min")]];
const PREF_KEY = "fbadmin.overview";

function loadPrefs(): { refresh: number; sort: string } {
  try {
    return { refresh: 60, sort: "status", ...JSON.parse(localStorage.getItem(PREF_KEY) ?? "{}") };
  } catch {
    return { refresh: 60, sort: "status" };
  }
}

function Badge(props: { level: Level }) {
  return <span className={`ov-badge ov-${props.level}`}><span aria-hidden="true">{LEVEL_ICON[props.level]}</span> {t(LEVEL_LABEL[props.level])}</span>;
}

function codeText(code: string | null, timeout: number): string | null {
  switch (code) {
    case "password_not_saved": return t("Password not saved: connect from the Object Explorer to check it");
    case "locked": return t("Saved password locked: enter the master password");
    case "timeout": return t("No answer within {seconds} s", { seconds: timeout });
    case "unreachable": return t("Unreachable");
    default: return null;
  }
}

function issueText(i: Issue): string {
  switch (i.code) {
    case "oat_gap": return t("Next transaction – OAT gap is {gap}", { gap: (i.value ?? 0).toLocaleString() });
    case "oit_gap": return t("OAT – OIT gap is {gap}", { gap: (i.value ?? 0).toLocaleString() });
    case "long_transactions": return t("{count} long-running transaction(s), oldest {age}", { count: i.value ?? 0, age: fmtAge(i.age) });
    case "nbackup_stalled": return t("Locked by nbackup (changes go to the delta file)");
    case "alert": return t("Alert rule firing: {rule}", { rule: i.rule ?? "" }) + (i.message ? ` — ${i.message}` : "");
    case "job_failed": return t("Scheduled job failed: {name}", { name: i.schedule ?? "" });
    default: return i.code;
  }
}

function openDashboard(app: AppApi, serverId: string, dbId?: string) {
  app.select(dbId ? { kind: "database", serverId, dbId } : { kind: "server", serverId });
  // show the Dashboard tab of the main panel for the new selection
  setTimeout(() => document.getElementById("tab-dashboard")?.click(), 0);
}

function since(ts: number | null | undefined): string {
  return ts ? fmtAge(Date.now() / 1000 - ts) : "–";
}

function DbRow(props: { srv: OverviewServer; d: OverviewDb; timeout: number }) {
  const app = useApp();
  const { srv, d } = props;
  const reachable = d.code === null;
  const gap = d.gaps?.next_oat;
  const repl = d.replication;
  const backup = [d.last_backup && { at: d.last_backup.at, what: t("scheduled: {name}", { name: d.last_backup.schedule }) },
    d.nbackup_last && { at: d.nbackup_last.at, what: t("nbackup level {level}", { level: d.nbackup_last.level }) }]
    .filter(Boolean).sort((a, b) => b!.at - a!.at)[0];
  return (
    <tr className={`ov-row ov-row-${d.status}`}>
      <td><Badge level={d.status} /></td>
      <td className="ov-dbname">
        <button className="linkish" onClick={() => openDashboard(app, srv.id, d.id)} title={d.path}>{d.name}</button>
        {d.code && <div className={d.code === "unreachable" || d.code === "timeout" ? "err small" : "muted small"}>{codeText(d.code, props.timeout)}{d.error ? `: ${d.error}` : ""}</div>}
        {d.issues.map((i, k) => <div key={k} className={`small ov-issue ov-${i.level}`}>{issueText(i)}</div>)}
      </td>
      <td>{reachable ? <>{d.engine_version}{d.ods ? <span className="muted"> · ODS {d.ods}</span> : null}</> : "–"}</td>
      <td className="num">{reachable ? fmtBytes(d.size_bytes) : "–"}{reachable && d.pages ? <div className="muted small">{t("{pages} pages", { pages: d.pages.toLocaleString() })}</div> : null}</td>
      <td className="num">{reachable ? d.attachments ?? "–" : "–"}</td>
      <td className="num">{reachable ? d.transactions ?? "–" : "–"}</td>
      <td className="num">{reachable && gap !== undefined ? gap.toLocaleString() : "–"}</td>
      <td className="num">{reachable ? (d.long_transactions ? <span className="warn">{d.long_transactions} · {fmtAge(d.oldest_transaction_age)}</span> : 0) : "–"}</td>
      <td>{reachable ? since(d.open_since) : "–"}</td>
      <td>{backup ? <>{t("{age} ago", { age: since(backup.at) })}<div className="muted small">{backup.what}</div></> : <span className="muted">{t("none known")}</span>}</td>
      <td>{repl ? (
        repl.replica_mode !== "none" ? <>{t("replica ({mode})", { mode: repl.replica_mode })}{repl.lag_segments !== undefined && <div className="small">{t("{count} segment(s) behind", { count: repl.lag_segments })}</div>}</>
          : repl.publication_enabled ? <>{t("primary")}<div className="muted small">{t("{count} published table(s)", { count: repl.published_tables })}</div></> : <span className="muted">{t("off")}</span>
      ) : <span className="muted">–</span>}</td>
      <td>
        <button className="btn small" onClick={() => openDashboard(app, srv.id, d.id)}>{t("Dashboard")}</button>{" "}
        {reachable && <button className="btn small" onClick={() => openPerformance(app, srv.id, d.id)}>{t("Performance")}</button>}
      </td>
    </tr>
  );
}

export function OverviewPanel(props: { visible: boolean }) {
  const app = useApp();
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Level[]>([]);
  const [text, setText] = useState("");
  const [prefs, setPrefsState] = useState(loadPrefs);
  const busy = useRef(false);
  const setPrefs = (p: Partial<typeof prefs>) => {
    const next = { ...prefs, ...p };
    setPrefsState(next);
    try { localStorage.setItem(PREF_KEY, JSON.stringify(next)); } catch { /* storage unavailable */ }
  };

  const load = async (refresh: boolean) => {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    try {
      setData(await oapi.get(refresh));
      setError(null);
    } catch (e) { setError(errMsg(e)); } finally { busy.current = false; setLoading(false); }
  };
  useEffect(() => { if (props.visible) load(false); }, [props.visible]);
  useEffect(() => {
    if (!props.visible || !prefs.refresh) return;
    const timer = setInterval(() => load(true), prefs.refresh * 1000);
    return () => clearInterval(timer);
  }, [props.visible, prefs.refresh]);

  const counts = useMemo(() => {
    const c: Record<Level, number> = { ok: 0, warning: 0, critical: 0, unknown: 0 };
    for (const s of data?.servers ?? []) {
      if (!s.databases.length) c[s.status]++;
      for (const d of s.databases) c[d.status]++;
    }
    return c;
  }, [data]);

  const servers = useMemo(() => {
    const words = text.toLowerCase().split(/\s+/).filter(Boolean);
    const match = (s: OverviewServer, d?: OverviewDb) => {
      const hay = [s.name, s.host, s.group, d?.name, d?.path].join(" ").toLowerCase();
      return words.every((w) => hay.includes(w));
    };
    const byStatus = (a: { status: Level; name: string }, b: { status: Level; name: string }) =>
      prefs.sort === "status" ? RANK[b.status] - RANK[a.status] || a.name.localeCompare(b.name) : a.name.localeCompare(b.name);
    return (data?.servers ?? []).map((s) => {
      const dbs = s.databases.filter((d) => (!filter.length || filter.includes(d.status)) && (match(s) || match(s, d)));
      const self = (!filter.length || filter.includes(s.status)) && match(s);
      return { s, dbs: [...dbs].sort(byStatus), show: dbs.length > 0 || (self && (!filter.length || !s.databases.length || s.code !== null)) };
    }).filter((x) => x.show).sort((a, b) => byStatus(a.s, b.s));
  }, [data, filter, text, prefs.sort]);

  const toggle = (lv: Level) => setFilter((f) => (f.includes(lv) ? f.filter((x) => x !== lv) : [...f, lv]));

  return (
    <div className="ov">
      <div className="toolbar">
        <span role="group" aria-label={t("Filter by status")} className="ov-filters">
          {LEVELS.map((lv) => (
            <button key={lv} className={`btn small ov-filter ov-${lv}` + (filter.includes(lv) ? " on" : "")} aria-pressed={filter.includes(lv)} onClick={() => toggle(lv)}>
              <span aria-hidden="true">{LEVEL_ICON[lv]}</span> {t(LEVEL_LABEL[lv])} <b>{counts[lv]}</b>
            </button>
          ))}
        </span>
        <input className="text" type="search" aria-label={t("Filter servers and databases")} placeholder={t("Filter by name, host, path…")}
          value={text} onChange={(e) => setText(e.target.value)} style={{ width: 220 }} />
        <label>{t("Sort")}
          <select className="text" value={prefs.sort} onChange={(e) => setPrefs({ sort: e.target.value })}>
            <option value="status">{t("Worst status first")}</option>
            <option value="name">{t("Name")}</option>
          </select>
        </label>
        <label>{t("Auto-refresh")}
          <select className="text" value={prefs.refresh} onChange={(e) => setPrefs({ refresh: Number(e.target.value) })}>
            {REFRESH.map(([v, l]) => <option key={v} value={v}>{t(l)}</option>)}
          </select>
        </label>
        <span className="grow" />
        <span className="muted" aria-live="polite">
          {loading ? t("Checking…") : data ? t("Checked at {time} in {ms} ms", { time: new Date(data.generated_at * 1000).toLocaleTimeString(), ms: data.elapsed_ms }) : ""}
        </span>
        <button className="btn small" disabled={loading} onClick={() => load(true)}><IconRefresh size={12} /> {t("Check now")}</button>
      </div>
      <div className="ov-body">
        {error && <div className="err" role="alert">{error}</div>}
        {data && data.servers.length === 0 && <div className="empty-state"><div className="big">{t("No servers registered")}</div><div>{t("Register a server to see its health here.")}</div></div>}
        {data && data.servers.length > 0 && servers.length === 0 && <div className="pad muted">{t("Nothing matches the filter.")}</div>}
        {servers.map(({ s, dbs }) => (
          <section key={s.id} className={`ov-server ov-border-${s.status}`} aria-labelledby={`ov-srv-${s.id}`}>
            <div className="ov-head">
              <Badge level={s.status} />
              <h3 id={`ov-srv-${s.id}`}><button className="linkish" onClick={() => openDashboard(app, s.id)}>{s.name}</button></h3>
              <span className="muted">{s.embedded ? t("embedded (local files)") : `${s.host}:${s.port}`}</span>
              {s.server_version && <span>{s.server_version}</span>}
              {s.server_attachments !== null && <span className="muted">{t("{count} attachment(s) on the server", { count: s.server_attachments })}</span>}
              {s.shared && <span className="pill">{t("shared")}</span>}
              {s.read_only && <span className="pill">{t("read-only")}</span>}
            </div>
            {s.code && <div className={s.code === "unreachable" || s.code === "timeout" ? "err ov-msg" : "muted ov-msg"}>{codeText(s.code, data?.timeout ?? 0)}{s.error ? `: ${s.error}` : ""}</div>}
            {s.databases.length === 0 ? <div className="muted ov-msg">{t("No databases registered on this server.")}</div> : dbs.length > 0 && (
              <div className="grid-wrap ov-grid">
                <table className="grid">
                  <thead><tr>
                    <th>{t("Status")}</th><th>{t("Database")}</th><th>{t("Version")}</th><th>{t("Size")}</th>
                    <th title={t("Attachments, not counting fbadmin's own check")}>{t("Attachments")}</th>
                    <th title={t("Active transactions, not counting fbadmin's own check")}>{t("Transactions")}</th>
                    <th title={t("Next transaction – oldest active transaction")}>{t("OAT gap")}</th>
                    <th title={t("Transactions running longer than {age}", { age: fmtAge(data?.thresholds.long_transaction) })}>{t("Long transactions")}</th>
                    <th title={t("Time since the oldest attachment connected (in SuperServer: since the database was opened)")}>{t("Open for")}</th>
                    <th>{t("Last backup")}</th><th>{t("Replication")}</th><th><span className="sr-only">{t("Actions")}</span></th>
                  </tr></thead>
                  <tbody>{dbs.map((d) => <DbRow key={d.id} srv={s} d={d} timeout={data?.timeout ?? 0} />)}</tbody>
                </table>
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

registerTabKind("overview", {
  icon: <IconActivity size={13} />,
  render: (_tab, visible) => <OverviewPanel visible={visible} />,
  same: () => true,
});

registerTopbar({
  id: "overview",
  render: (app) => <button className="tb" onClick={() => app.openTab({ kind: "overview", title: t("Overview") })}>▦ {t("Overview")}</button>,
});

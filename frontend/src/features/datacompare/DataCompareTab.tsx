import { useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import type { Job } from "../../types";
import { ConfirmDialog } from "../../components/dialogs";
import { IconDownload, IconPlay, IconRefresh, IconSpinner, IconTerminal, IconTrash } from "../../components/Icons";
import { JobLog } from "../maintenance/common";
import { N_, t } from "../../i18n";
import { dcApi, type CompareResult, type Endpoint, type Listing, type RunSummary, type SampleRow, type Samples, type TableResult, type TablePlan } from "./api";

type Kind = "insert" | "update" | "delete";
const KIND_LABEL: Record<Kind, string> = {
  insert: N_("Only in source (INSERT)"),
  update: N_("Different (UPDATE)"),
  delete: N_("Only in target (DELETE)"),
};
const MAX_EDITOR_SCRIPT = 2 << 20; // larger scripts are only offered as a download
const TABLE_STATUSES = [N_("pending"), N_("running"), N_("cancelled")]; // shown with t(status)
void TABLE_STATUSES;
const fmtWhen = (ts: number) => new Date(ts * 1000).toLocaleString();

const splitList = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

/** Server + database selects with accessible names. */
function Picker(props: { label: string; value: Endpoint; onChange: (v: Endpoint) => void }) {
  const app = useApp();
  const srv = app.servers.find((s) => s.id === props.value.server_id);
  return (
    <span className="sch-picker" role="group" aria-label={props.label}>
      <b>{props.label}</b>
      <select className="text" aria-label={t("{side} server", { side: props.label })} value={props.value.server_id}
        onChange={(e) => {
          const s = app.servers.find((x) => x.id === e.target.value);
          props.onChange({ server_id: e.target.value, db_id: s?.databases[0]?.id ?? "" });
        }}>
        {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <select className="text" aria-label={t("{side} database", { side: props.label })} value={props.value.db_id}
        onChange={(e) => props.onChange({ ...props.value, db_id: e.target.value })}>
        {!props.value.db_id && <option value="">—</option>}
        {(srv?.databases ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
    </span>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  return String(v);
}

function TableNotes({ p }: { p: Pick<TablePlan, "source_only" | "target_only" | "type_mismatch" | "warnings" | "self_references"> }) {
  const notes: string[] = [];
  for (const r of p.self_references ?? []) notes.push(t("References itself ({fk}): rows are inserted parents first and deleted children first", { fk: r.name }));
  if (p.source_only.length) notes.push(t("only in source: {cols}", { cols: p.source_only.join(", ") }));
  if (p.target_only.length) notes.push(t("only in target: {cols}", { cols: p.target_only.join(", ") }));
  for (const m of p.type_mismatch) notes.push(t("{col}: {source} → {target}", { col: m.column, source: m.source, target: m.target }));
  for (const w of p.warnings) notes.push(t(w));
  if (!notes.length) return <span className="muted">—</span>;
  return <ul className="dc-notes">{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>;
}

function SampleGrid({ samples, kind, total }: { samples: Samples; kind: Kind; total: number }) {
  const rows: SampleRow[] = samples[kind];
  const cols = samples.columns;
  const nk = samples.key.length;
  if (!total) return <div className="pad muted">{t("No rows")}</div>;
  return (
    <div className="grid-wrap dc-samples">
      <table className="grid">
        <thead>
          <tr>
            {kind === "update" && <th>{t("Side")}</th>}
            {cols.map((c, i) => <th key={c} className={i < nk ? "dc-key" : ""}>{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => kind === "update" ? (
            [["source", r.source], ["target", r.target]].map(([side, vals]) => (
              <tr key={`${i}-${side}`} className={"dc-" + side + (side === "target" ? " dc-pair-end" : "")}>
                <td className="muted">{side === "source" ? t("source (new)") : t("target (old)")}</td>
                {(vals as unknown[]).map((v, j) => {
                  const chg = r.changed.includes(cols[j]);
                  return <td key={j} className={chg ? (side === "source" ? "dc-new" : "dc-old") : (side === "target" && j >= nk ? "dc-same" : "")}
                    title={fmtCell(v)}>{fmtCell(v)}</td>;
                })}
              </tr>
            ))
          ) : (
            <tr key={i}>
              {((kind === "insert" ? r.source : r.target) ?? []).map((v, j) => (
                <td key={j} className={kind === "insert" ? "dc-new" : "dc-old"} title={fmtCell(v)}>{fmtCell(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length < total && (
        <div className="pad muted">{t("Showing {n} of {total} rows (raise “Sample rows per table” to keep more).", { n: rows.length, total })}</div>
      )}
    </div>
  );
}

function Details({ jobId, table }: { jobId: string; table: TableResult }) {
  const [samples, setSamples] = useState<Samples | null>(null);
  const [error, setError] = useState<string | null>(null);
  const counts: Record<Kind, number> = { insert: table.only_source, update: table.different, delete: table.only_target };
  const first = (["update", "insert", "delete"] as Kind[]).find((k) => counts[k] > 0) ?? "update";
  const [kind, setKind] = useState<Kind>(first);
  useEffect(() => {
    setSamples(null);
    setKind(first);
    dcApi.samples(jobId, table.name).then(setSamples).catch((e) => setError(errMsg(e)));
  }, [jobId, table.name]);
  return (
    <section className="dc-details" aria-label={t("Rows of {table}", { table: table.name })}>
      <h3>{t("Rows of {table}", { table: table.name })}</h3>
      <div className="sch-row">
        <div className="seg" role="group" aria-label={t("Kind of difference")}>
          {(["insert", "update", "delete"] as Kind[]).map((k) => (
            <button key={k} className={kind === k ? "on" : ""} aria-pressed={kind === k} onClick={() => setKind(k)}>
              {t(KIND_LABEL[k])} <b>{counts[k].toLocaleString()}</b>
            </button>
          ))}
        </div>
        {Object.keys(table.changed_columns).length > 0 && (
          <span className="muted">{t("Changed columns")}: {Object.entries(table.changed_columns).map(([c, n]) => `${c} (${n})`).join(", ")}</span>
        )}
      </div>
      {error && <div className="err">{error}</div>}
      {samples ? <SampleGrid samples={samples} kind={kind} total={counts[kind]} /> : !error && <IconSpinner />}
    </section>
  );
}

export function DataCompareTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const [source, setSource] = useState<Endpoint>({ server_id: tab.serverId ?? app.servers[0]?.id ?? "", db_id: tab.dbId ?? "" });
  const [target, setTarget] = useState<Endpoint>(() => {
    const srv = app.servers.find((s) => s.id === source.server_id);
    return { server_id: source.server_id, db_id: srv?.databases.find((d) => d.id !== source.db_id)?.id ?? "" };
  });
  const [listing, setListing] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [where, setWhere] = useState<Record<string, string>>({});
  const [inserts, setInserts] = useState(true);
  const [updates, setUpdates] = useState(true);
  const [deletes, setDeletes] = useState(true);
  const [ignore, setIgnore] = useState("");
  const [maxDiffs, setMaxDiffs] = useState(100);
  const [job, setJob] = useState<Job | null>(null); // started in this tab (live log, Cancel)
  const [runId, setRunId] = useState<string | null>(null); // the comparison shown (also a saved one)
  const [res, setRes] = useState<CompareResult | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [savedLog, setSavedLog] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [applyJob, setApplyJob] = useState<Job | null>(null);

  const same = source.server_id === target.server_id && source.db_id === target.db_id;
  const targetServer = app.servers.find((s) => s.id === target.server_id);
  const targetDb = targetServer?.databases.find((d) => d.id === target.db_id);

  const load = async () => {
    if (!source.db_id || !target.db_id || same) { setListing(null); return; }
    setLoading(true);
    setListError(null);
    try {
      const l = await dcApi.tables(source, target);
      setListing(l);
      const pre = new Set<string>(tab.props?.tables ?? []);
      setChecked(Object.fromEntries(l.tables.filter((x) => x.comparable).map((x) => [x.name, pre.size ? pre.has(x.name) : true])));
    } catch (e) {
      setListing(null);
      setListError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [source.server_id, source.db_id, target.server_id, target.db_id]);

  const comparable = useMemo(() => listing?.tables.filter((x) => x.comparable) ?? [], [listing]);
  const others = useMemo(() => listing?.tables.filter((x) => !x.comparable) ?? [], [listing]);
  const chosen = comparable.filter((x) => checked[x.name]);
  const running = res?.status === "running";

  const loadRuns = () => dcApi.runs().then(setRuns).catch(() => undefined);
  useEffect(() => { loadRuns(); }, []);

  // live result while the comparison runs
  useEffect(() => {
    if (!runId) return;
    let live = true;
    setSavedLog(null);
    const tick = async () => {
      try {
        const r = await dcApi.result(runId);
        if (!live) return;
        setRes(r);
        if (r.status === "running") setTimeout(tick, 700);
        else loadRuns();
      } catch (e) {
        if (live) setError(errMsg(e));
      }
    };
    tick();
    return () => { live = false; };
  }, [runId]);

  const exists = (ep: Endpoint) => !!app.servers.find((s) => s.id === ep.server_id)?.databases.some((d) => d.id === ep.db_id);
  const openRun = (r: RunSummary) => {
    setError(null);
    setSel(null);
    setApplyJob(null);
    setJob(null);
    setRes(null);
    const src = { server_id: r.source[0], db_id: r.source[1] };
    const tgt = { server_id: r.target[0], db_id: r.target[1] };
    if (exists(src) && exists(tgt)) { setSource(src); setTarget(tgt); }
    setRunId(r.job_id);
  };
  const deleteRun = async (r: RunSummary) => {
    try {
      await dcApi.deleteRun(r.job_id);
      if (runId === r.job_id) { setRunId(null); setRes(null); setJob(null); }
      loadRuns();
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
  };

  const start = async () => {
    setError(null);
    setSel(null);
    setApplyJob(null);
    try {
      const j = await dcApi.compare({
        source, target,
        tables: chosen.map((x) => ({ name: x.name, where: where[x.name]?.trim() || null })),
        ignore_columns: splitList(ignore), inserts, updates, deletes, max_differences: maxDiffs,
      });
      setRes(null);
      setJob(j);
      setRunId(j.id);
    } catch (e) {
      setError(errMsg(e));
    }
  };

  const openInQueryTool = async () => {
    try {
      const r = await fetch(dcApi.scriptUrl(res!.job_id));
      if (!r.ok) throw new Error(r.statusText);
      app.openQueryTool(target.server_id, target.db_id, await r.text());
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
  };

  const confirmApply = () => app.showDialog(
    <ConfirmDialog
      title={t("Apply the sync script to the target")}
      message={t("Run {n} statement(s) on {target} in one transaction? It is rolled back if any statement fails. Triggers on the target fire as usual.", { n: res!.statements, target: res!.target_name })}
      action={t("Apply to target")}
      confirmText={resServer?.databases.find((d) => d.id === res!.target.db_id)?.name ?? targetDb?.name ?? ""}
      onConfirm={async () => { setApplyJob(await dcApi.apply(res!.target, res!.job_id)); }}
    />,
  );

  const selTable = res?.tables.find((x) => x.name === sel) ?? null;
  const finished = res?.status === "finished";
  const resServer = res ? app.servers.find((s) => s.id === res.target.server_id) : undefined;
  const resReadOnly = !!resServer?.read_only;
  const resTargetGone = !!res && !exists(res.target);

  return (
    <div className="sch-col sch-scroll dc">
      <div className="sch-steps">
        <section>
          <h3>{t("Compare data")}</h3>
          <div className="sch-row">
            <Picker label={t("Source")} value={source} onChange={setSource} />
            <span className="sch-arrow" aria-hidden="true">→</span>
            <Picker label={t("Target")} value={target} onChange={setTarget} />
            <button className="btn small" onClick={load} disabled={loading} title={t("Reload the tables")} aria-label={t("Reload the tables")}>
              {loading ? <IconSpinner /> : <IconRefresh />}
            </button>
          </div>
          <p className="muted">{t("Rows are matched by primary key (or a unique key). The sync script makes the target's rows equal to the source: INSERT for rows only in the source, UPDATE for changed rows, DELETE for rows only in the target.")}</p>
          {same && <div className="err">{t("Source and target are the same database")}</div>}
          {listError && <div className="err">{listError}</div>}
          {runs.length > 0 && (
            <details className="dc-runs">
              <summary>{t("Recent comparisons ({n})", { n: runs.length })}</summary>
              <div className="grid-wrap">
                <table className="grid">
                  <thead><tr><th>{t("Compared")}</th><th>{t("Source")}</th><th>{t("Target")}</th><th>{t("Status")}</th>
                    <th className="num">{t("Statements")}</th><th><span className="sr-only">{t("Actions")}</span></th></tr></thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.job_id} className={r.job_id === runId ? "sel" : ""} aria-current={r.job_id === runId ? "true" : undefined}>
                        <td>{fmtWhen(r.created)}</td>
                        <td>{r.source_name}</td>
                        <td>{r.target_name}</td>
                        <td><span className={"pill " + r.status}>{t(r.status)}</span>{r.applied && <span className="muted"> · {t("applied")}</span>}</td>
                        <td className="num">{r.status === "finished" ? r.statements.toLocaleString() : "—"}</td>
                        <td className="dc-run-actions">
                          <button className="btn small" onClick={() => openRun(r)} disabled={r.job_id === runId}>{t("Open")}</button>
                          <button className="btn small danger" onClick={() => deleteRun(r)} disabled={r.status === "running"}
                            aria-label={t("Delete the comparison of {time}", { time: fmtWhen(r.created) })}><IconTrash size={12} /></button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </section>

        {listing && (
          <section>
            <h3>{t("Tables")} <span className="muted">{t("{n} of {total}", { n: chosen.length, total: comparable.length })}</span></h3>
            <div className="sch-row">
              <button className="btn small" onClick={() => setChecked(Object.fromEntries(comparable.map((x) => [x.name, true])))}>{t("All")}</button>
              <button className="btn small" onClick={() => setChecked({})}>{t("None")}</button>
            </div>
            <div className="grid-wrap dc-tables">
              <table className="grid">
                <thead>
                  <tr><th><span className="sr-only">{t("Compare")}</span></th><th>{t("Table")}</th><th>{t("Key")}</th>
                    <th>{t("Columns")}</th><th>{t("Notes")}</th><th>{t("WHERE filter (both sides)")}</th></tr>
                </thead>
                <tbody>
                  {comparable.map((x) => (
                    <tr key={x.name}>
                      <td><input type="checkbox" aria-label={t("Compare {table}", { table: x.name })} checked={!!checked[x.name]}
                        onChange={(e) => setChecked({ ...checked, [x.name]: e.target.checked })} /></td>
                      <td className="mono">{x.name}</td>
                      <td className="mono" title={x.key_type ?? ""}>{x.key.join(", ")}{x.key_type === "UNIQUE" && <span className="muted"> ({t("unique")})</span>}</td>
                      <td className="num">{x.columns.length}</td>
                      <td><TableNotes p={x} /></td>
                      <td><input className="text mono dc-where" placeholder={t("e.g. ID > 1000")} aria-label={t("WHERE filter for {table}", { table: x.name })}
                        value={where[x.name] ?? ""} onChange={(e) => setWhere({ ...where, [x.name]: e.target.value })} /></td>
                    </tr>
                  ))}
                  {!comparable.length && <tr><td colSpan={6} className="muted">{t("No table can be compared")}</td></tr>}
                </tbody>
              </table>
            </div>
            {others.length > 0 && (
              <details className="dc-others">
                <summary>{t("Not comparable ({n})", { n: others.length })}</summary>
                <ul>{others.map((x) => <li key={x.name}><span className="mono">{x.name}</span> — {t(x.reason ?? "")}</li>)}</ul>
              </details>
            )}
          </section>
        )}

        {listing && (
          <section>
            <h3>{t("Options")}</h3>
            <div className="sch-row">
              <span>{t("Statements in the script")}:</span>
              <label><input type="checkbox" checked={inserts} onChange={(e) => setInserts(e.target.checked)} /> INSERT</label>
              <label><input type="checkbox" checked={updates} onChange={(e) => setUpdates(e.target.checked)} /> UPDATE</label>
              <label><input type="checkbox" checked={deletes} onChange={(e) => setDeletes(e.target.checked)} /> DELETE</label>
            </div>
            <div className="sch-row">
              <label>{t("Ignore columns")} <input className="text mono" style={{ width: 300 }} value={ignore} placeholder={t("COLUMN or TABLE.COLUMN, comma-separated")}
                onChange={(e) => setIgnore(e.target.value)} /></label>
              <label>{t("Sample rows per table")} <input className="text" type="number" min={0} max={10000} style={{ width: 90 }} value={maxDiffs}
                onChange={(e) => setMaxDiffs(Math.max(0, Math.min(10000, Number(e.target.value) || 0)))} /></label>
            </div>
            <div className="sch-row">
              <button className="btn primary" disabled={!chosen.length || same || running} onClick={start}>
                {running ? <IconSpinner /> : <IconPlay />} {t("Compare {n} table(s)", { n: chosen.length })}
              </button>
              {error && <span className="err">{error}</span>}
            </div>
          </section>
        )}

        {runId && (
          <section>
            <h3>{t("Result")} {res && <span className="muted">{res.source_name} → {res.target_name} · {t("compared {time}", { time: fmtWhen(res.created) })}</span>}</h3>
            {res?.status === "cancelled" && <div className="muted">{t("The comparison was cancelled: the results are incomplete and there is no sync script.")}</div>}
            {res?.status === "failed" && res.error && <div className="err">{res.error}</div>}
            {res && (
              <>
                <div className="grid-wrap dc-results">
                  <table className="grid">
                    <thead>
                      <tr><th>{t("Table")}</th><th className="num">{t("Only in source")}</th><th className="num">{t("Different")}</th>
                        <th className="num">{t("Only in target")}</th><th className="num">{t("Identical")}</th><th>{t("Changed columns")}</th><th>{t("Status")}</th></tr>
                    </thead>
                    <tbody>
                      {res.tables.map((x) => (
                        <tr key={x.name} className={sel === x.name ? "sel" : ""} aria-current={sel === x.name ? "true" : undefined}>
                          <td><button className="linkish mono" onClick={() => setSel(x.name)} disabled={x.status !== "done"}
                            aria-label={t("Show rows of {table}", { table: x.name })}>{x.name}</button></td>
                          <td className={"num" + (x.only_source ? " dc-n-ins" : "")}>{x.only_source.toLocaleString()}</td>
                          <td className={"num" + (x.different ? " dc-n-upd" : "")}>{x.different.toLocaleString()}</td>
                          <td className={"num" + (x.only_target ? " dc-n-del" : "")}>{x.only_target.toLocaleString()}</td>
                          <td className="num">{x.identical.toLocaleString()}</td>
                          <td className="mono">{Object.keys(x.changed_columns).join(", ")}</td>
                          <td>
                            {x.status === "error" ? <span className="err" title={x.error ?? ""}>{t("error")}: {x.error}</span>
                              : x.status === "done" ? <span className="dc-ok">{(x.only_source || x.only_target || x.different) ? t("differences") : t("identical")}</span>
                                : <span className="muted">{t(x.status)}</span>}
                            {x.ordering === "python" && <span className="muted" title={t("The server's ORDER BY did not match a binary ordering of the keys (collation), so the rows were sorted in fbadmin")}> · {t("sorted in fbadmin")}</span>}
                            {x.null_keys > 0 && <span className="muted"> · {t("{n} row(s) with NULL key skipped", { n: x.null_keys })}</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {res.skipped.length > 0 && <div className="muted">{t("Skipped")}: {res.skipped.map((s) => `${s.name} (${t(s.reason)})`).join(", ")}</div>}
                {finished && (
                  <div className="sch-row dc-actions">
                    <b>{t("Sync script: {n} statement(s)", { n: res.statements.toLocaleString() })}</b>
                    <a className="btn" href={dcApi.scriptUrl(res.job_id)} download><IconDownload /> {t("Download script")}</a>
                    <button className="btn" disabled={!res.statements || res.script_bytes > MAX_EDITOR_SCRIPT} onClick={openInQueryTool}
                      title={res.script_bytes > MAX_EDITOR_SCRIPT ? t("The script is too large for the editor: download it") : t("Review and run the script on the target")}>
                      <IconTerminal /> {t("Open in Query Tool (target)")}</button>
                    <button className="btn danger" disabled={!res.statements || resReadOnly || resTargetGone || res.applied || !!applyJob} onClick={confirmApply}
                      title={resReadOnly ? t("The target server is registered as read-only") : t("Run the script on the target in one transaction")}>
                      <IconPlay /> {t("Apply to target…")}</button>
                    {resReadOnly && <span className="muted">{t("The target server is registered as read-only")}</span>}
                    {res.applied && <span className="muted">{t("This script was applied to the target.")}</span>}
                    {!job && !res.applied && <span className="muted">{t("Compared {time}: the target may have changed since. Applying stops (and rolls back) if a row to update or delete is gone.", { time: fmtWhen(res.created) })}</span>}
                  </div>
                )}
              </>
            )}
            {job ? <JobLog job={job} maxHeight={160} onDone={() => dcApi.result(job.id).then(setRes).catch(() => undefined)} />
              : (
                <details className="dc-log" onToggle={(e) => {
                  if ((e.target as HTMLDetailsElement).open && savedLog === null) dcApi.log(runId).then((l) => setSavedLog(l.log)).catch(() => setSavedLog([]));
                }}>
                  <summary>{t("Log")}</summary>
                  <div className="log" style={{ maxHeight: 160 }}>{savedLog === null ? "…" : savedLog.join("\n") || t("(no output)")}</div>
                </details>
              )}
            {applyJob && (
              <>
                <JobLog job={applyJob} maxHeight={160} onDone={(j) => {
                  if (j.status === "finished") app.toast(t("The target was updated"));
                  dcApi.result(res!.job_id).then(setRes).catch(() => undefined);
                  loadRuns();
                }} />
                <div className="sch-row"><button className="btn" onClick={start} disabled={running}><IconRefresh /> {t("Compare again")}</button></div>
              </>
            )}
            {selTable && finished && <Details jobId={res!.job_id} table={selTable} />}
          </section>
        )}
      </div>
    </div>
  );
}

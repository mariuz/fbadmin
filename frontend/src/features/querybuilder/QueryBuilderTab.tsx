/**
 * Visual query builder (pgAdmin parity): pick tables and views, joins (suggested from foreign keys),
 * output columns with aliases, aggregates (GROUP BY added automatically), WHERE / HAVING criteria,
 * ORDER BY, DISTINCT and a row limit, with a live SQL preview, Run, and "Open in Query Tool".
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp, type WorkTab } from "../../context";
import { t } from "../../i18n";
import { Modal } from "../../components/Modal";
import { ResultGrid } from "../../components/ResultGrid";
import { IconCopy, IconPlay, IconPlus, IconRefresh, IconSpinner, IconTerminal, IconTrash } from "../../components/Icons";
import type { ScriptResult } from "../../types";
import { qbApi, type RelationList } from "./api";
import { BOX_W, Canvas, boxHeight } from "./Canvas";
import { CriteriaGrid, FieldsGrid, JoinsGrid, TablesGrid } from "./Grids";
import {
  emptyState, generateSql, newId, setReservedWords, stateFromSql, suggestAlias, suggestJoins, withState,
  type Issue, type LimitStyle, type QbState, type QbTable,
} from "./model";

type Pane = "columns" | "criteria" | "joins" | "tables" | "results";
const RUN_ROWS = 500;

function issueText(i: Issue): string {
  switch (i.code) {
    case "no_tables": return t("Add a table or view to start.");
    case "duplicate_alias": return t("The alias {alias} is used more than once.", { alias: i.detail ?? "" });
    case "join_ignored": return t("A join condition could not be placed: {condition}", { condition: i.detail ?? "" });
    case "join_without_condition": return t("The join to {table} has no condition (ON 1 = 1).", { table: i.detail ?? "" });
    case "cross_join": return t("{table} is not joined to the other tables: CROSS JOIN (every combination of rows).", { table: i.detail ?? "" });
    case "star_with_aggregate": return t("“All columns” cannot be combined with aggregates.");
    case "distinct_order_not_selected": return t("With DISTINCT, Firebird requires ORDER BY columns to be in the output.");
    case "rows_zero": return t("ROWS cannot return zero rows; use FETCH or FIRST for a limit of 0.");
    default: return i.code;
  }
}

export function QueryBuilderTab({ tab, visible }: { tab: WorkTab; visible: boolean }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const storeKey = `fbadmin.qb.${sid}.${dbid}`;
  const [st, setSt] = useState<QbState>(emptyState);
  const [meta, setMeta] = useState<RelationList | null>(null);
  const [schema, setSchema] = useState<string | null>(tab.schema ?? null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState("");
  const [pane, setPane] = useState<Pane>("columns");
  const [activeJoin, setActiveJoin] = useState<string | null>(null);
  const [saveState, setSaveState] = useState(false);
  const [params, setParams] = useState<Record<string, string>>({});
  const [run, setRun] = useState<{ running: boolean; result?: ScriptResult; error?: string } | null>(null);
  const [loadOpen, setLoadOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const session = useRef<string | null>(null);
  const restored = useRef(false);
  const update = useCallback((fn: (s: QbState) => QbState) => setSt(fn), []);

  // relation list (per schema on Firebird 6)
  useEffect(() => {
    let stop = false;
    qbApi.relations(sid, dbid, schema).then((m) => {
      if (stop) return;
      setReservedWords(m.reserved);
      setMeta(m);
      if (m.schemas_supported && !schema && m.schema) setSchema(m.schema);
      setError(null);
    }).catch((e) => !stop && setError(errMsg(e)));
    return () => { stop = true; };
  }, [sid, dbid, schema, reload]);

  const fetchTable = async (name: string, sch: string | null, kind: "table" | "view", taken: string[], x: number, y: number, alias?: string): Promise<QbTable> => {
    const info = await qbApi.relation(sid, dbid, name, sch);
    return {
      id: newId("t"), schema: info.schema, name, kind, alias: alias ?? suggestAlias(name, taken), columns: info.columns,
      foreign_keys: info.foreign_keys, x, y,
    };
  };

  /** Load columns and foreign keys of tables restored without them (saved state). */
  const rehydrate = async (saved: QbState): Promise<QbState> => {
    const tables: QbTable[] = [];
    const missing: string[] = [];
    for (const tb of saved.tables) {
      try {
        const info = await qbApi.relation(sid, dbid, tb.name, tb.schema);
        tables.push({ ...tb, columns: info.columns, foreign_keys: info.foreign_keys });
      } catch {
        missing.push(tb.name);
      }
    }
    if (missing.length) app.toast(t("Not found and left out: {tables}", { tables: missing.join(", ") }), "error");
    const ids = new Set(tables.map((x) => x.id));
    return {
      ...saved, tables,
      joins: saved.joins.filter((j) => ids.has(j.left) && ids.has(j.right)),
      fields: saved.fields.filter((f) => !f.table || ids.has(f.table)),
      criteria: saved.criteria.filter((c) => !c.table || ids.has(c.table)),
    };
  };

  // restore the last builder state of this database; "Query builder" on a table adds that table
  const startTable = () => {
    const name = tab.props?.table as string | undefined;
    if (name) addTable(name, tab.props?.schema ?? tab.schema ?? null, tab.props?.kind === "view" ? "view" : "table");
  };
  useEffect(() => {
    (async () => {
      let saved: QbState | null = null;
      try { saved = JSON.parse(localStorage.getItem(storeKey) || "null"); } catch { /* ignore */ }
      if (saved?.version === 1 && saved.tables?.length) setSt(await rehydrate({ ...emptyState(), ...saved }));
      restored.current = true;
      startTable();
    })();
    return () => { if (session.current) api.closeSession(session.current).catch(() => {}); };
  }, []);
  const nonce = useRef(tab.props?.nonce);
  useEffect(() => {
    if (tab.props?.nonce === nonce.current) return;
    nonce.current = tab.props?.nonce;
    startTable();
  }, [tab.props?.nonce]);
  useEffect(() => {
    if (!restored.current) return;
    try {
      localStorage.setItem(storeKey, JSON.stringify({ ...st, tables: st.tables.map((x) => ({ ...x, columns: [], foreign_keys: [] })) }));
    } catch { /* storage unavailable */ }
  }, [st]);

  const engine = meta?.engine_major ?? 5;
  const gen = useMemo(() => generateSql(st, engine), [st, engine]);

  const addTable = async (name: string, sch: string | null, kind: "table" | "view", at?: { x: number; y: number }) => {
    try {
      const tb = await fetchTable(name, sch, kind, [], 0, 0);
      setSt((s) => {
        const taken = s.tables.map((o) => o.alias);
        let x = at?.x ?? 30 + s.tables.reduce((m, o) => Math.max(m, o.x + BOX_W + 70), 0);
        let y = at?.y ?? 30;
        if (!at && x > 30 + 3 * (BOX_W + 70)) { // wrap to a new row after four boxes
          x = 30;
          y = 30 + Math.max(...s.tables.map((o) => o.y + boxHeight(o))) + 40;
        }
        const t2 = { ...tb, x, y, alias: suggestAlias(name, taken) };
        return { ...s, tables: [...s.tables, t2], joins: [...s.joins, ...suggestJoins(t2, s.tables)] };
      });
    } catch (e) {
      app.toast(errMsg(e), "error");
    }
  };

  const removeTable = (id: string) => setSt((s) => ({
    ...s, tables: s.tables.filter((x) => x.id !== id), joins: s.joins.filter((j) => j.left !== id && j.right !== id),
    fields: s.fields.filter((f) => f.table !== id), criteria: s.criteria.filter((c) => c.table !== id),
  }));
  const toggleColumn = (tableId: string, column: string) => setSt((s) => {
    const has = s.fields.find((f) => f.table === tableId && f.column === column && f.output);
    if (has) return { ...s, fields: s.fields.filter((f) => f !== has) };
    return { ...s, fields: [...s.fields, { id: newId("f"), table: tableId, column, expr: "", agg: "", alias: "", output: true, sort: "", sortOrder: 1 + Math.max(0, ...s.fields.map((x) => x.sortOrder)) }] };
  });
  const allColumns = (tableId: string) => setSt((s) => {
    const tb = s.tables.find((x) => x.id === tableId);
    if (!tb) return s;
    const have = new Set(s.fields.filter((f) => f.table === tableId).map((f) => f.column));
    const extra = tb.columns.filter((c) => !have.has(c.name)).map((c, i) => ({
      id: newId("f"), table: tableId, column: c.name, expr: "", agg: "" as const, alias: "", output: true, sort: "" as const, sortOrder: s.fields.length + i + 1,
    }));
    return { ...s, fields: [...s.fields, ...extra] };
  });
  const joinColumns = (a: { table: string; column: string }, b: { table: string; column: string }) => {
    setSt((s) => {
      const existing = s.joins.find((j) => (j.left === a.table && j.right === b.table) || (j.left === b.table && j.right === a.table));
      if (existing) {
        const cond = existing.left === a.table ? { left: a.column, op: "=", right: b.column } : { left: b.column, op: "=", right: a.column };
        return { ...s, joins: s.joins.map((j) => (j === existing ? { ...j, conditions: [...j.conditions.filter((c) => c.left && c.right), cond] } : j)) };
      }
      return { ...s, joins: [...s.joins, { id: newId("j"), type: "INNER", left: a.table, right: b.table, conditions: [{ left: a.column, op: "=", right: b.column }], custom: "" }] };
    });
  };
  const editJoin = (id: string) => {
    setActiveJoin(id);
    setPane("joins");
    setTimeout(() => document.getElementById(`qb-join-${id}`)?.querySelector("select")?.focus(), 0);
  };

  const finalSql = () => (saveState ? withState(gen.sql, st) : gen.sql + "\n");
  const openInQueryTool = () => {
    const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
    app.openTab({ kind: "query", title: db?.name ?? "query", serverId: sid, dbId: dbid, initialSql: finalSql(), schema: schema ?? undefined });
  };
  const runQuery = async () => {
    if (!gen.sql) return;
    setPane("results");
    setRun({ running: true });
    try {
      if (!session.current) session.current = (await api.openSession(sid, dbid, schema ?? undefined)).session_id;
      // the server names the parameters (upper case for unquoted :names); match ours case-insensitively
      const names = gen.params.length ? (await api.queryParams(gen.sql)).params : [];
      const value = (n: string) => params[gen.params.find((p) => p.toUpperCase() === n.toUpperCase()) ?? n] ?? "";
      const body = {
        sql: gen.sql, autocommit: true, max_rows: RUN_ROWS,
        params: names.length ? Object.fromEntries(names.map((n) => [n, value(n)])) : undefined,
      };
      let r: ScriptResult;
      try {
        r = await api.runQuery(session.current, body);
      } catch (e: any) {
        if (e?.status !== 404) throw e;
        session.current = (await api.openSession(sid, dbid, schema ?? undefined)).session_id;
        r = await api.runQuery(session.current, body);
      }
      setRun({ running: false, result: r, error: r.error?.message });
    } catch (e) {
      setRun({ running: false, error: errMsg(e) });
    }
  };
  const loadFromSql = async (text: string): Promise<string | null> => {
    const saved = stateFromSql(text);
    if (!saved) return t("This SQL has no saved builder state. The builder reopens only queries it generated with “Save builder state in the SQL” ticked; it does not parse other SELECT statements.");
    setSt(await rehydrate(saved));
    return null;
  };

  const tables = meta?.relations.filter((r) => r.kind === "table") ?? [];
  const views = meta?.relations.filter((r) => r.kind === "view") ?? [];
  const result = run?.result?.results.filter((r) => r.kind === "select").slice(-1)[0];

  return (
    <div className="qb" style={{ display: visible ? "flex" : "none" }}>
      <div className="toolbar">
        {meta?.schemas_supported && (
          <label>{t("Schema")}
            <select className="text" value={schema ?? ""} onChange={(e) => setSchema(e.target.value)}>
              {meta.schemas.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        )}
        <label>{t("Add table")}
          <select className="text qb-pick" value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">{t("— pick a table or view —")}</option>
            {tables.length > 0 && <optgroup label={t("Tables")}>{tables.map((r) => <option key={r.name} value={`table:${r.name}`}>{r.name}</option>)}</optgroup>}
            {views.length > 0 && <optgroup label={t("Views")}>{views.map((r) => <option key={r.name} value={`view:${r.name}`}>{r.name}</option>)}</optgroup>}
          </select>
        </label>
        <button className="btn small" disabled={!pick} onClick={() => {
          const [kind, ...rest] = pick.split(":");
          addTable(rest.join(":"), meta?.schema ?? null, kind as "table" | "view");
          setPick("");
        }}><IconPlus size={12} /> {t("Add")}</button>
        <button className="btn icon small" aria-label={t("Refresh the list of tables")} title={t("Refresh the list of tables")} onClick={() => setReload((n) => n + 1)}><IconRefresh size={13} /></button>
        <span className="sep" />
        <label className="check"><input type="checkbox" checked={st.distinct} onChange={(e) => setSt((s) => ({ ...s, distinct: e.target.checked }))} /> DISTINCT</label>
        <span className="sep" />
        <label title={t("Row limit syntax")}>{t("Limit")}
          <select className="text" value={st.limitStyle} onChange={(e) => setSt((s) => ({ ...s, limitStyle: e.target.value as LimitStyle }))}>
            <option value="fetch" disabled={engine < 3}>OFFSET / FETCH</option>
            <option value="rows">ROWS m TO n</option>
            <option value="first">FIRST / SKIP</option>
          </select>
        </label>
        <label>{t("Rows")}
          <input className="text" type="number" min={0} style={{ width: 70 }} value={st.limit ?? ""} placeholder={t("all")}
            onChange={(e) => setSt((s) => ({ ...s, limit: e.target.value === "" ? null : Math.max(0, Number(e.target.value)) }))} />
        </label>
        <label>{t("Skip")}
          <input className="text" type="number" min={0} style={{ width: 70 }} value={st.offset ?? ""} placeholder="0"
            onChange={(e) => setSt((s) => ({ ...s, offset: e.target.value === "" ? null : Math.max(0, Number(e.target.value)) }))} />
        </label>
        <span className="grow" />
        <button className="btn small" onClick={() => setLoadOpen(true)}>{t("Load from SQL…")}</button>
        <button className="btn small" disabled={!st.tables.length} onClick={() => { if (window.confirm(t("Remove all tables, joins, columns and conditions?"))) setSt(emptyState()); }}><IconTrash size={13} /> {t("Clear")}</button>
      </div>
      {error && <div className="err" style={{ padding: "6px 10px" }}>{error}</div>}
      <div className="qb-top">
        <Canvas st={st} update={update} activeJoin={activeJoin}
          onDropRelation={(r, x, y) => addTable(r.name, r.schema ?? meta?.schema ?? null, r.kind, { x, y })}
          onToggleColumn={toggleColumn} onRemoveTable={removeTable} onJoinColumns={joinColumns} onEditJoin={editJoin} />
        <section className="qb-sql" aria-label={t("SQL preview")}>
          <div className="qb-sql-bar">
            <b>SQL</b>
            <span className="grow" />
            <button className="btn small primary" disabled={!gen.sql || run?.running} onClick={runQuery} title={t("Run the query (first {count} rows)", { count: RUN_ROWS })}>
              {run?.running ? <IconSpinner size={13} className="spin" /> : <IconPlay size={13} />} {t("Run")}
            </button>
            <button className="btn small" disabled={!gen.sql} onClick={openInQueryTool}><IconTerminal size={13} /> {t("Open in Query Tool")}</button>
            <button className="btn icon small" disabled={!gen.sql} aria-label={t("Copy SQL")} title={t("Copy SQL")}
              onClick={() => navigator.clipboard?.writeText(finalSql()).then(() => app.toast(t("SQL copied")), () => {})}><IconCopy size={13} /></button>
          </div>
          <pre className="mono qb-sql-text" tabIndex={0} aria-label={t("Generated SQL")}>{gen.sql || t("-- add a table to start")}</pre>
          <label className="check qb-save"><input type="checkbox" checked={saveState} onChange={(e) => setSaveState(e.target.checked)} /> {t("Save builder state in the SQL (a comment the builder can load again)")}</label>
          {gen.params.length > 0 && (
            <fieldset className="qb-params">
              <legend>{t("Parameters for Run")}</legend>
              {gen.params.map((p) => (
                <label key={p}><span className="mono">:{p}</span>
                  <input className="text mono" value={params[p] ?? ""} onChange={(e) => setParams({ ...params, [p]: e.target.value })} />
                </label>
              ))}
            </fieldset>
          )}
          {gen.issues.filter((i) => i.code !== "no_tables").length > 0 && (
            <ul className="qb-issues">
              {gen.issues.filter((i) => i.code !== "no_tables").map((i, k) => <li key={k} className="warn">{issueText(i)}</li>)}
            </ul>
          )}
        </section>
      </div>
      <div className="qb-bottom">
        <div className="subtabs" role="tablist" aria-label={t("Query parts")}>
          {([["columns", t("Columns ({count})", { count: st.fields.length })], ["criteria", t("Conditions ({count})", { count: st.criteria.length })],
            ["joins", t("Joins ({count})", { count: st.joins.length })], ["tables", t("Tables ({count})", { count: st.tables.length })],
            ["results", t("Results")]] as Array<[Pane, string]>).map(([k, label]) => (
            <button key={k} role="tab" aria-selected={pane === k} className={pane === k ? "active" : ""} onClick={() => setPane(k)}>{label}</button>
          ))}
        </div>
        <div className="qb-pane" role="tabpanel">
          {pane === "columns" && <FieldsGrid st={st} update={update} />}
          {pane === "criteria" && <CriteriaGrid st={st} update={update} />}
          {pane === "joins" && <JoinsGrid st={st} update={update} active={activeJoin} />}
          {pane === "tables" && <TablesGrid st={st} update={update} onRemove={removeTable} onAllColumns={allColumns} />}
          {pane === "results" && (
            !run ? <div className="qb-empty muted">{t("Click Run to see the first {count} rows.", { count: RUN_ROWS })}</div>
              : run.running ? <div className="qb-empty"><IconSpinner className="spin" /> {t("Running…")}</div>
              : run.error ? <div className="err" style={{ padding: 10, whiteSpace: "pre-wrap" }}>{run.error}</div>
              : result ? <div style={{ height: "100%" }}><ResultGrid columns={result.columns!} rows={result.rows!} name="query" hasMore={false} /></div>
              : <div className="qb-empty muted">{t("The statement returned no rows.")}</div>
          )}
        </div>
      </div>
      {loadOpen && <LoadDialog onClose={() => setLoadOpen(false)} onLoad={loadFromSql} />}
    </div>
  );
}

function LoadDialog(props: { onClose: () => void; onLoad: (sql: string) => Promise<string | null> }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title={t("Load builder state from SQL")} onClose={props.onClose} wide
      footer={<>
        <button className="btn" onClick={props.onClose}>{t("Cancel")}</button>
        <button className="btn primary" disabled={!text.trim()} onClick={async () => {
          const e = await props.onLoad(text);
          if (e) setErr(e); else props.onClose();
        }}>{t("Load")}</button>
      </>}>
      <label className="qb-load">
        {t("Paste a query saved by the query builder (with its “fbadmin:query-builder” comment):")}
        <textarea className="text mono" rows={10} autoFocus value={text} onChange={(e) => { setText(e.target.value); setErr(null); }} />
      </label>
      {err && <div className="err" role="alert" style={{ marginTop: 8 }}>{err}</div>}
    </Modal>
  );
}

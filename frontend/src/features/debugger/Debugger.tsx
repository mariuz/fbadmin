import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { errMsg, useApp, type WorkTab } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { IconActivity, IconPlay, IconPlus, IconRefresh, IconTrash } from "../../components/Icons";
import { N_, t } from "../../i18n";
import { Section, useBusy } from "../maintenance/common";
import {
  dapi, type DebugVar, type ProfileResult, type RoutineInfo, type StepKind, type TestCase, type TestResult,
  type TraceResult,
} from "./api";

const KIND_LABEL: Record<StepKind, string> = {
  entry: N_("start"), step: N_("step"), row: N_("row out"), end: N_("end"), handler: N_("caught error"), fetch: N_("fetch"),
};
const ROUTINE_KIND: Record<string, string> = {
  selectable: N_("selectable procedure"), executable: N_("executable procedure"), function: N_("function"),
};
const BLOCK_TEMPLATE = `EXECUTE BLOCK (N INTEGER = ?)
RETURNS (I INTEGER, SQUARE INTEGER)
AS
BEGIN
  I = 0;
  WHILE (I < N) DO
  BEGIN
    I = I + 1;
    SQUARE = I * I;
    SUSPEND;
  END
END`;

export function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

function fmtNs(ns: number | undefined | null): string {
  if (ns === null || ns === undefined) return "";
  if (ns < 1e3) return `${ns} ns`;
  if (ns < 1e6) return `${(ns / 1e3).toFixed(1)} µs`;
  if (ns < 1e9) return `${(ns / 1e6).toFixed(2)} ms`;
  return `${(ns / 1e9).toFixed(2)} s`;
}

/** Scroll ``el`` into view inside its scroll container only (scrollIntoView would also scroll the panels). */
function scrollWithin(container: HTMLElement | null, el: HTMLElement | null) {
  if (!container || !el) return;
  const c = container.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const head = 28; // sticky header rows
  if (r.top < c.top + head) container.scrollTop -= c.top + head - r.top;
  else if (r.bottom > c.bottom) container.scrollTop += r.bottom - c.bottom;
}

type Inputs = Record<string, string | null>;

function InputsForm(props: { vars: DebugVar[]; value: Inputs; onChange: (v: Inputs) => void; idPrefix: string }) {
  if (!props.vars.length) return <div className="muted">{t("No input parameters.")}</div>;
  const set = (name: string, v: string | null) => props.onChange({ ...props.value, [name]: v });
  return (
    <table className="dbg-inputs">
      <tbody>
        {props.vars.map((p) => {
          const v = props.value[p.name];
          const id = `${props.idPrefix}-${p.name}`;
          return (
            <tr key={p.name}>
              <td><label htmlFor={id} className="mono">{p.name}</label><div className="muted small">{p.type}</div></td>
              <td><input id={id} className="text mono" value={v ?? ""} placeholder={v === null ? "NULL" : t("(empty string)")}
                onChange={(e) => set(p.name, e.target.value)} /></td>
              <td><label className="check small"><input type="checkbox" checked={v === null}
                onChange={(e) => set(p.name, e.target.checked ? null : "")} /> NULL</label></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function defaultInputs(vars: DebugVar[], prev?: Inputs): Inputs {
  const out: Inputs = {};
  for (const p of vars) out[p.name] = prev && p.name in prev ? prev[p.name] : null;
  return out;
}

// ----------------------------------------------------------------------------------- source
function SourceView(props: {
  source: string; current: number | null; errorLine: number | null; hits: Map<number, number>;
  profile: ProfileResult | null; onLine: (line: number) => void;
}) {
  const lines = props.source.split("\n");
  const prof = new Map<number, { count: number; total_ns: number }>();
  for (const l of props.profile?.lines ?? []) prof.set(l.line, l);
  const max = Math.max(1, ...[...prof.values()].map((x) => x.total_ns));
  const curRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => { scrollWithin(boxRef.current?.parentElement ?? null, curRef.current); }, [props.current]);
  const showProf = !!props.profile?.available;
  return (
    <div className={"dbg-src" + (showProf ? " prof" : "")} role="list" aria-label={t("Source")} ref={boxRef}>
      <div className="ln head" role="listitem">
        <span className="no">{t("Line")}</span><span className="hits">{t("Steps")}</span>
        {showProf && <><span className="c">{t("Count")}</span><span className="t">{t("Time")}</span></>}
        <span className="code" />
      </div>
      {lines.map((text, i) => {
        const n = i + 1;
        const h = props.hits.get(n);
        const p = prof.get(n);
        const cls = "ln" + (props.current === n ? " cur" : "") + (props.errorLine === n ? " errline" : "")
          + (h ? " hit" : "") + (p && p.total_ns / max > 0.25 ? " hot" : "");
        return (
          <div key={i} className={cls} ref={props.current === n ? curRef : undefined} role="listitem">
            <span className="no">
              {h ? <button className="linkbtn" onClick={() => props.onLine(n)} aria-label={t("Go to the next step at line {line}", { line: n })}>{n}</button> : n}
            </span>
            <span className="hits">{h ?? ""}</span>
            {showProf && <><span className="c">{p ? p.count : ""}</span><span className="t">{p ? fmtNs(p.total_ns) : ""}</span></>}
            <span className="code">{p && <span className="heat" style={{ width: `${(p.total_ns / max) * 100}%` }} />}{text || " "}</span>
          </div>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------------------------------------- results
function ResultTable(props: { columns: string[]; rows: unknown[][]; label: string }) {
  if (!props.rows.length) return <div className="muted">{t("No rows.")}</div>;
  return (
    <div className="grid-wrap" style={{ maxHeight: 220 }}>
      <table className="grid" aria-label={props.label}>
        <thead><tr><th className="rn">#</th>{props.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {props.rows.map((r, i) => (
            <tr key={i}><td className="rn">{i + 1}</td>{r.map((v, j) => <td key={j} className={v === null ? "dbg-null" : ""}>{fmtVal(v)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StepList(props: { trace: TraceResult; cur: number; setCur: (i: number) => void; source: string[] }) {
  const { steps } = props.trace;
  const WINDOW = 400;
  const from = Math.max(0, Math.min(props.cur - WINDOW / 2, steps.length - WINDOW));
  const to = Math.min(steps.length, from + WINDOW);
  const curRef = useRef<HTMLTableRowElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => { scrollWithin(boxRef.current, curRef.current); }, [props.cur]);
  const onKey = (e: KeyboardEvent) => {
    const go = { ArrowDown: props.cur + 1, ArrowUp: props.cur - 1, Home: 0, End: steps.length - 1, PageDown: props.cur + 20, PageUp: props.cur - 20 }[e.key];
    if (go !== undefined) {
      e.preventDefault();
      props.setCur(Math.max(0, Math.min(steps.length - 1, go)));
    }
  };
  return (
    <div className="grid-wrap dbg-steps" ref={boxRef} tabIndex={0} onKeyDown={onKey} aria-label={t("Recorded steps (arrow keys move)")} role="region">
      {from > 0 && <div className="muted small pad-s">{t("{count} earlier steps", { count: from })}</div>}
      <table className="grid">
        <thead><tr><th className="rn">#</th><th>{t("Kind")}</th><th>{t("Line")}</th><th>{t("Statement")}</th></tr></thead>
        <tbody>
          {steps.slice(from, to).map((s, k) => {
            const i = from + k;
            return (
              <tr key={i} ref={i === props.cur ? curRef : undefined} className={(i === props.cur ? "sel " : "") + "k-" + s.k}
                onClick={() => props.setCur(i)} aria-selected={i === props.cur}>
                <td className="rn">{i + 1}</td>
                <td className="nowrap">{t(KIND_LABEL[s.k] ?? s.k)}{s.row ? ` #${s.row}` : ""}</td>
                <td className="num">{s.line ?? ""}</td>
                <td className="mono stmt">{(props.source[(s.line ?? 0) - 1] ?? "").trim()}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {to < steps.length && <div className="muted small pad-s">{t("{count} later steps", { count: steps.length - to })}</div>}
    </div>
  );
}

function VarsTable(props: { trace: TraceResult; cur: number }) {
  const step = props.trace.steps[props.cur];
  if (!step) return <div className="muted">{t("Run a trace to see the variables.")}</div>;
  const changed = new Set(step.changed ?? []);
  return (
    <div className="grid-wrap" style={{ maxHeight: 300 }}>
      <table className="grid" aria-label={t("Variables")}>
        <thead><tr><th>{t("Variable")}</th><th>{t("Kind")}</th><th>{t("Value")}</th></tr></thead>
        <tbody>
          {props.trace.variables.map((v, i) => {
            const val = step.v[i];
            return (
              <tr key={v.name + v.kind} className={changed.has(i) ? "changed" : ""}>
                <td className="mono" title={v.type}>{v.name}</td>
                <td className="muted">{t(v.kind)}</td>
                <td className={"mono" + (val === null ? " dbg-null" : "")}>{fmtVal(val)}{changed.has(i) ? <span className="sr-only"> {t("(changed)")}</span> : null}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {step.caught && (
        <div className="dbg-caught">
          <b>{t("Caught by a WHEN handler")}</b> GDSCODE {step.caught.gdscode ?? "?"}, SQLSTATE {step.caught.sqlstate ?? "?"}
          {step.caught.message && <pre>{step.caught.message}</pre>}
        </div>
      )}
    </div>
  );
}
N_("input"); N_("local"); N_("output"); N_("result");

// ----------------------------------------------------------------------------------- trace view
function TraceView(props: {
  sid: string; dbid: string; otype: "procedures" | "functions" | "block"; name?: string; visible: boolean;
  onSaveTest: (inputs: Inputs, trace: TraceResult | null) => void;
}) {
  const { sid, dbid, otype, name } = props;
  const app = useApp();
  const [info, setInfo] = useState<RoutineInfo | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [inputs, setInputs] = useState<Inputs>({});
  const [blockSql, setBlockSql] = useState(BLOCK_TEMPLATE);
  const blockRef = useRef(BLOCK_TEMPLATE);
  const [traced, setTraced] = useState<{ result: TraceResult; source: string } | null>(null);
  const [cur, setCur] = useState(0);
  const [profile, setProfile] = useState<ProfileResult | null>(null);
  const [showSql, setShowSql] = useState(false);
  const [maxSteps, setMaxSteps] = useState(10000);
  const { busy, error, setError, run } = useBusy();

  const load = async () => {
    setLoadErr(null);
    try {
      const i = otype === "block" ? await dapi.parse(sid, dbid, blockRef.current) : await dapi.routine(sid, dbid, otype, name!);
      setInfo(i);
      setInputs((prev) => defaultInputs(i.inputs, prev));
      return i;
    } catch (e) {
      setLoadErr(errMsg(e));
      return null;
    }
  };
  useEffect(() => { load(); }, [sid, dbid, otype, name]);

  const body = (i: RoutineInfo) => ({
    otype, name: name ?? null, sql: otype === "block" ? blockRef.current : null,
    inputs: Object.fromEntries(i.inputs.map((p) => [p.name, inputs[p.name] ?? null])), max_steps: maxSteps,
  });

  const doTrace = () => run(async () => {
    const i = otype === "block" ? await load() : info;
    if (!i) return;
    if (!i.traceable) throw new Error(i.reason ?? t("This routine cannot be traced"));
    const r = await dapi.trace(sid, dbid, body(i));
    setTraced({ result: r, source: otype === "block" ? blockRef.current : i.source ?? "" });
    const errIdx = r.steps.length - 1;
    setCur(r.error ? Math.max(0, errIdx) : 0);
    setProfile(null);
  });
  const doProfile = () => run(async () => {
    const i = otype === "block" ? await load() : info;
    if (!i) return;
    const p = await dapi.profile(sid, dbid, body(i));
    if (!p.available) { app.toast(p.reason ?? t("The profiler is not available"), "info"); return; }
    setProfile(p);
  });

  const tr = traced?.result ?? null;
  const steps = tr?.steps ?? [];
  const step = steps[cur];
  const source = traced?.source ?? (otype === "block" ? blockSql : info?.source ?? "");
  const srcLines = useMemo(() => source.split("\n"), [source]);
  const hits = useMemo(() => {
    const m = new Map<number, number>();
    for (const s of steps) if (s.line) m.set(s.line, (m.get(s.line) ?? 0) + 1);
    return m;
  }, [tr]);
  const goLine = (line: number) => {
    for (let k = 1; k <= steps.length; k++) {
      const i = (cur + k) % steps.length;
      if (steps[i].line === line) { setCur(i); return; }
    }
  };
  const nav = (i: number) => setCur(Math.max(0, Math.min(steps.length - 1, i)));
  const errorLine = tr?.error?.line ?? (info && !info.traceable ? info.error_line ?? null : null);
  const profAvail = info?.profiler;

  if (loadErr && otype !== "block") return <div className="pad err" role="alert">{loadErr}</div>;
  return (
    <div className="dbg">
      <div className="toolbar" role="toolbar" aria-label={t("Debugger")}>
        <button className="btn primary" disabled={busy || (!!info && !info.traceable && otype !== "block")} onClick={doTrace}
          title={t("Run the instrumented copy and record every step (rolled back)")}><IconPlay /> {t("Run trace")}</button>
        <button className="btn" disabled={busy || !info || (otype !== "block" && info.external)} onClick={doProfile}
          title={profAvail ? t("Run the routine itself under the Firebird 5 profiler (rolled back)") : t("Per-line timings need Firebird 5")}>
          <IconActivity /> {t("Profile")}</button>
        <span className="sep" />
        <button className="btn icon" disabled={!steps.length || cur === 0} onClick={() => nav(0)} aria-label={t("First step")} title={t("First step")}>⏮</button>
        <button className="btn icon" disabled={!steps.length || cur === 0} onClick={() => nav(cur - 1)} aria-label={t("Step back")} title={t("Step back")}>◀</button>
        <button className="btn icon" disabled={!steps.length || cur >= steps.length - 1} onClick={() => nav(cur + 1)} aria-label={t("Step forward")} title={t("Step forward")}>▶</button>
        <button className="btn icon" disabled={!steps.length || cur >= steps.length - 1} onClick={() => nav(steps.length - 1)} aria-label={t("Last step")} title={t("Last step")}>⏭</button>
        <span className="muted" aria-live="polite">{steps.length ? t("Step {n} of {count}", { n: cur + 1, count: steps.length }) + (step?.line ? " · " + t("line {line}", { line: step.line }) : "") : ""}</span>
        <span className="sep" />
        <label>{t("Max steps")} <input className="text" type="number" min={1} max={100000} style={{ width: 80 }} value={maxSteps}
          onChange={(e) => setMaxSteps(Number(e.target.value) || 10000)} /></label>
        <span className="grow" />
        {otype !== "block" && <button className="btn" disabled={!info} onClick={() => info && props.onSaveTest(inputs, tr)}><IconPlus /> {t("Save as test…")}</button>}
        {tr && <button className={"btn" + (showSql ? " on" : "")} aria-pressed={showSql} onClick={() => setShowSql(!showSql)}>{t("Instrumented SQL")}</button>}
        <button className="btn icon" onClick={load} aria-label={t("Reload")} title={t("Reload the routine")}><IconRefresh /></button>
      </div>
      <div className="dbg-note" role="note">
        {t("Each run uses its own connection and ONE transaction that is rolled back: writes done by the routine are undone. "
          + "Not undone: IN AUTONOMOUS TRANSACTION blocks and EXECUTE STATEMENT ... ON EXTERNAL (refused in the traced routine), "
          + "sequence increments, external tables and effects of external (UDR) routines. Called procedures, functions and triggers run normally and are not traced.")}
        {info && <span className="muted"> · {otype === "block" ? "EXECUTE BLOCK" : t(ROUTINE_KIND[info.kind] ?? info.kind)}{info.engine_version ? ` · Firebird ${info.engine_version}` : ""}</span>}
      </div>
      {[...(info?.warnings ?? []), ...(tr?.warnings ?? []).filter((w) => !info?.warnings.includes(w))].map((w, i) => <div key={i} className="dbg-warn" role="note">⚠ {w}</div>)}
      {info && !info.traceable && <div className="dbg-warn err" role="alert">{info.reason}</div>}
      {otype === "block" && loadErr && <div className="dbg-warn err" role="alert">{loadErr}</div>}
      {error && <div className="dbg-warn err" role="alert" style={{ whiteSpace: "pre-wrap" }}>{error}<button className="btn small" style={{ marginLeft: 8 }} onClick={() => setError(null)}>{t("Dismiss")}</button></div>}
      {!profAvail && info && <div className="muted small dbg-pad">{t("Per-line timings (Profile) need Firebird 5 or newer.")}</div>}
      <div className="dbg-main">
        <div className="dbg-left">
          {otype === "block" && (
            <div className="editor-box dbg-editor" aria-label={t("EXECUTE BLOCK")}>
              <SqlEditor value={blockSql} onChange={(x) => { blockRef.current = x; setBlockSql(x); }} onExecute={doTrace} />
            </div>
          )}
          <div className="dbg-srcbox">
            {showSql && tr ? <pre className="dbg-sql" aria-label={t("Instrumented SQL")}>{tr.instrumented_sql}</pre> : (
              source ? <SourceView source={source} current={step?.line ?? null} errorLine={errorLine} hits={hits} profile={profile} onLine={goLine} />
                : <div className="muted pad">{info?.external ? info.reason : t("Loading…")}</div>
            )}
          </div>
          {profile?.available && (
            <div className="muted small dbg-pad">{t("Profile #{id}: {time} in total; the profiled run called the routine itself (rolled back).", { id: profile.profile_id ?? "", time: fmtNs(profile.total_ns) })}</div>
          )}
          {tr && tr.steps.length > 0 && (
            <div className="dbg-stepsbox">
              <StepList trace={tr} cur={cur} setCur={setCur} source={srcLines} />
            </div>
          )}
        </div>
        <div className="dbg-right">
          <Section title={t("Inputs")}>
            {info ? <InputsForm vars={info.inputs} value={inputs} onChange={setInputs} idPrefix={`dbg-in-${otype}-${name ?? "block"}`} /> : <div className="muted">{t("Loading…")}</div>}
          </Section>
          <Section title={step ? t("Variables after step {n} (line {line})", { n: cur + 1, line: step.line ?? "-" }) : t("Variables")}>
            {tr ? <VarsTable trace={tr} cur={cur} /> : <div className="muted">{t("Run a trace to see the variables.")}</div>}
          </Section>
          {tr && (
            <Section title={t("Result")} right={<span className="muted small">{t("{ms} ms, rolled back", { ms: tr.elapsed_ms })}</span>}>
              {tr.error && (
                <div className="dbg-error" role="alert">
                  <pre>{tr.error.message}</pre>
                  {tr.error.last_line ? <button className="btn small" onClick={() => nav(steps.length - 1)}>{t("Show the last step (line {line})", { line: tr.error.last_line })}</button> : null}
                </div>
              )}
              {tr.stopped && <div className="warn">{t("Stopped after {count} steps (is there an endless loop?). Raise “Max steps” to record more.", { count: steps.length })}</div>}
              {!tr.compiled && <div className="muted">{t("The instrumented copy did not compile; see “Instrumented SQL”.")}</div>}
              {tr.compiled && !tr.error && !tr.stopped && (info?.kind === "function"
                ? <div>{t("Returned")}: <b className="mono">{fmtVal(tr.result)}</b></div>
                : tr.columns.length ? <ResultTable columns={tr.columns} rows={tr.rows} label={t("Output rows")} /> : <div className="muted">{t("Finished (no output parameters).")}</div>)}
            </Section>
          )}
          {profile?.run && (
            <Section title={t("Profiled run")}>
              {profile.run.error ? <pre className="err">{profile.run.error}</pre> : <ResultTable columns={profile.run.columns} rows={profile.run.rows} label={t("Rows of the profiled run")} />}
            </Section>
          )}
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------------- tests
interface Draft {
  id?: string; otype: "procedures" | "functions"; routine: string; name: string; inputs: Inputs;
  mode: "rows" | "error"; rows: string; error: string;
}

function draftFrom(otype: "procedures" | "functions", routine: string, inputs: Inputs, tr: TraceResult | null): Draft {
  const name = Object.entries(inputs).map(([k, v]) => `${k}=${v ?? "NULL"}`).join(", ") || t("no inputs");
  if (tr?.error) return { otype, routine, name, inputs, mode: "error", rows: "[]", error: tr.error.message.split("\n")[0] };
  return { otype, routine, name, inputs, mode: "rows", rows: JSON.stringify(tr && !tr.stopped ? tr.rows : []), error: "" };
}

function TestsView(props: {
  sid: string; dbid: string; otype?: "procedures" | "functions"; name?: string; draft: Draft | null;
  setDraft: (d: Draft | null) => void; visible: boolean;
}) {
  const { sid, dbid, otype, name } = props;
  const [tests, setTests] = useState<TestCase[]>([]);
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [summary, setSummary] = useState<Record<string, number> | null>(null);
  const [info, setInfo] = useState<RoutineInfo | null>(null);
  const { busy, error, run } = useBusy();
  const app = useApp();
  const load = async () => {
    try { setTests(await dapi.tests(sid, dbid, otype, name)); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  useEffect(() => { if (props.visible) load(); }, [props.visible, sid, dbid, otype, name]);
  useEffect(() => {
    if (otype && name) dapi.routine(sid, dbid, otype, name).then(setInfo).catch(() => setInfo(null));
  }, [sid, dbid, otype, name]);

  const runTests = (body: { ids?: string[]; otype?: string; routine?: string }) => run(async () => {
    const r = await dapi.runTests(sid, dbid, body as never);
    setResults((cur) => ({ ...cur, ...Object.fromEntries(r.results.map((x) => [x.id, x])) }));
    setSummary(r.summary);
    await load();
  });
  const save = () => run(async () => {
    const d = props.draft!;
    let rows: unknown[][] | null = null;
    if (d.mode === "rows") {
      try { rows = JSON.parse(d.rows || "[]"); } catch { throw new Error(t("Expected rows must be JSON: a list of rows, e.g. [[1, \"a\"], [2, null]]")); }
      if (!Array.isArray(rows) || !rows.every(Array.isArray)) throw new Error(t("Expected rows must be a list of rows (lists), e.g. [[1, \"a\"]]"));
    }
    const expect = d.mode === "rows" ? { rows, error: null } : { rows: null, error: d.error };
    if (d.id) await dapi.updateTest(sid, dbid, d.id, { name: d.name, inputs: d.inputs, expect });
    else await dapi.addTest(sid, dbid, { otype: d.otype, routine: d.routine, name: d.name, inputs: d.inputs, expect });
    props.setDraft(null);
    await load();
  }, t("Test saved"));
  const del = (tc: TestCase) => run(async () => {
    if (!confirm(t("Delete test “{name}”?", { name: tc.name }))) return;
    await dapi.deleteTest(sid, dbid, tc.id);
    await load();
  });
  const edit = (tc: TestCase) => props.setDraft({
    id: tc.id, otype: tc.otype, routine: tc.routine, name: tc.name, inputs: { ...tc.inputs },
    mode: tc.expect.error !== null && tc.expect.error !== undefined ? "error" : "rows",
    rows: JSON.stringify(tc.expect.rows ?? []), error: tc.expect.error ?? "",
  });
  const d = props.draft;
  const draftVars: DebugVar[] = d ? (info && d.routine === name ? info.inputs : Object.keys(d.inputs).map((k) => ({ name: k, type: "", kind: "input" }))) : [];
  return (
    <div className="pad dbg-tests">
      <div className="toolbar dbg-tests-bar" role="toolbar" aria-label={t("Tests")}>
        {otype && name && <button className="btn" disabled={busy} onClick={() => props.setDraft(draftFrom(otype, name, defaultInputs(info?.inputs ?? []), null))}><IconPlus /> {t("New test")}</button>}
        {otype && name && <button className="btn primary" disabled={busy || !tests.length} onClick={() => runTests({ otype, routine: name })}><IconPlay /> {t("Run tests of {name}", { name })}</button>}
        <button className={"btn" + (otype ? "" : " primary")} disabled={busy} onClick={() => runTests({})}><IconPlay /> {t("Run all tests of this database")}</button>
        <span className="grow" />
        {summary && <span aria-live="polite"><span className="pill dbg-pass">{t("{n} passed", { n: summary.pass ?? 0 })}</span> <span className="pill dbg-fail">{t("{n} failed", { n: summary.fail ?? 0 })}</span> {summary.error ? <span className="pill dbg-fail">{t("{n} errors", { n: summary.error })}</span> : null}</span>}
        <button className="btn icon" onClick={load} aria-label={t("Reload")} title={t("Reload")}><IconRefresh /></button>
      </div>
      <p className="muted small">{t("Tests call the routine itself with the saved inputs, one transaction per test, always rolled back, and compare the rows (or the error) with the expected outcome. They are stored by fbadmin (psql_tests.json in its data directory), not in the database.")}</p>
      {error && <div className="err" role="alert" style={{ whiteSpace: "pre-wrap" }}>{error}</div>}
      {d && (
        <Section title={d.id ? t("Edit test") : t("New test of {name}", { name: d.routine })}>
          <div className="form">
            <label className="l" htmlFor="dbg-test-name">{t("Name")}</label>
            <input id="dbg-test-name" className="text" value={d.name} onChange={(e) => props.setDraft({ ...d, name: e.target.value })} />
            <span className="l muted">{t("Inputs")}</span>
            <InputsForm vars={draftVars} value={d.inputs} onChange={(v) => props.setDraft({ ...d, inputs: v })} idPrefix="dbg-test-in" />
            <span className="l muted">{t("Expect")}</span>
            <div className="seg" role="radiogroup" aria-label={t("Expect")} style={{ justifySelf: "start" }}>
              <button role="radio" aria-checked={d.mode === "rows"} className={d.mode === "rows" ? "on" : ""} onClick={() => props.setDraft({ ...d, mode: "rows" })}>{t("Rows")}</button>
              <button role="radio" aria-checked={d.mode === "error"} className={d.mode === "error" ? "on" : ""} onClick={() => props.setDraft({ ...d, mode: "error" })}>{t("An error")}</button>
            </div>
            {d.mode === "rows" ? <>
              <label className="l" htmlFor="dbg-test-rows">{t("Expected rows (JSON)")}</label>
              <textarea id="dbg-test-rows" className="text mono" rows={4} value={d.rows} onChange={(e) => props.setDraft({ ...d, rows: e.target.value })}
                placeholder='[[1, "a"], [2, null]]' />
            </> : <>
              <label className="l" htmlFor="dbg-test-error">{t("Error message contains")}</label>
              <input id="dbg-test-error" className="text" value={d.error} placeholder={t("(empty: any error)")} onChange={(e) => props.setDraft({ ...d, error: e.target.value })} />
            </>}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button className="btn primary" disabled={busy || !d.name.trim()} onClick={save}>{t("Save test")}</button>
            <button className="btn" onClick={() => props.setDraft(null)}>{t("Cancel")}</button>
          </div>
        </Section>
      )}
      {tests.length === 0 ? <div className="muted">{otype ? t("No tests for this routine yet: run a trace and use “Save as test…”, or “New test”.") : t("No PSQL tests in this database yet.")}</div> : (
        <div className="grid-wrap" style={{ maxHeight: "none" }}>
          <table className="grid" aria-label={t("Tests")}>
            <thead><tr>{!otype && <th>{t("Routine")}</th>}<th>{t("Test")}</th><th>{t("Inputs")}</th><th>{t("Expected")}</th><th>{t("Status")}</th><th>{t("Actions")}</th></tr></thead>
            <tbody>
              {tests.map((tc) => {
                const r = results[tc.id];
                const status = r?.status ?? tc.last_status;
                return (
                  <tr key={tc.id}>
                    {!otype && <td className="mono">{tc.routine}</td>}
                    <td>{tc.name}</td>
                    <td className="mono small">{Object.entries(tc.inputs).map(([k, v]) => `${k}=${v ?? "NULL"}`).join(", ")}</td>
                    <td className="mono small">{tc.expect.error !== null && tc.expect.error !== undefined ? t("error containing “{text}”", { text: tc.expect.error }) : JSON.stringify(tc.expect.rows ?? [])}</td>
                    <td>
                      {status ? <span className={"pill " + (status === "pass" ? "dbg-pass" : "dbg-fail")}>{t(status)}</span> : <span className="muted">—</span>}
                      {r && r.diffs.length > 0 && <ul className="dbg-diffs">{r.diffs.map((x, i) => <li key={i}>{x}</li>)}</ul>}
                    </td>
                    <td className="nowrap">
                      <button className="btn small" disabled={busy} onClick={() => runTests({ ids: [tc.id] })} aria-label={t("Run test {name}", { name: tc.name })}><IconPlay size={12} /></button>{" "}
                      <button className="btn small" onClick={() => edit(tc)} aria-label={t("Edit test {name}", { name: tc.name })}>{t("Edit")}</button>{" "}
                      <button className="btn small danger" disabled={busy} onClick={() => del(tc)} aria-label={t("Delete test {name}", { name: tc.name })}><IconTrash size={12} /></button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
N_("pass"); N_("fail"); N_("error");

// ----------------------------------------------------------------------------------- tab
export function DebugTab(props: { tab: WorkTab; visible: boolean }) {
  const { serverId: sid, dbId: dbid } = props.tab;
  const p = props.tab.props ?? {};
  const otype: "procedures" | "functions" | "block" | undefined = p.otype;
  const name: string | undefined = p.name;
  const routineTests = otype === "procedures" || otype === "functions";
  const [view, setView] = useState<"trace" | "tests">(otype ? (p.view === "tests" && routineTests ? "tests" : "trace") : "tests");
  const [draft, setDraft] = useState<Draft | null>(null);
  useEffect(() => { if (p.view && otype && (p.view === "trace" || routineTests)) setView(p.view); }, [p.at]);
  if (!sid || !dbid) return null;
  return (
    <div className="dbg-tab">
      {otype && routineTests && (
        <div className="subtabs dbg-subtabs" role="tablist" aria-label={t("Debugger views")}>
          <button role="tab" aria-selected={view === "trace"} className={view === "trace" ? "active" : ""} onClick={() => setView("trace")}>{t("Trace")}</button>
          <button role="tab" aria-selected={view === "tests"} className={view === "tests" ? "active" : ""} onClick={() => setView("tests")}>{t("Tests")}</button>
        </div>
      )}
      <div className="dbg-body" style={{ display: view === "trace" && otype ? "flex" : "none" }}>
        {otype && <TraceView sid={sid} dbid={dbid} otype={otype} name={name} visible={props.visible && view === "trace"}
          onSaveTest={(inputs, tr) => { if (routineTests) { setDraft(draftFrom(otype, name!, inputs, tr)); setView("tests"); } }} />}
      </div>
      {view === "tests" && (
        <TestsView sid={sid} dbid={dbid} otype={routineTests ? otype : undefined} name={routineTests ? name : undefined}
          draft={draft} setDraft={setDraft} visible={props.visible} />
      )}
    </div>
  );
}

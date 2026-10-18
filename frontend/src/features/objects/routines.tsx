import { useEffect, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import { api } from "../../api";
import { errMsg, keys, useApp, type WorkTab } from "../../context";
import { SqlEditor, clearErrors, lineColToOffset, showError } from "../../components/SqlEditor";
import { IconPlay, IconPlus, IconRefresh, IconTerminal, IconTrash } from "../../components/Icons";
import { CREATE_TEMPLATES } from "../../templates";
import { typeInfo } from "../../types";
import type { Dependent } from "./api";
import {
  Area, Check, DependentsNote, Field, ObjectDialog, SecuritySelect, Select, Text, move, quote, useDependents,
  useLookups, useSpec,
} from "./common";
import { t } from "../../i18n";

interface Param { name: string; type: string; comment?: string | null }
interface Ext { name?: string | null; engine?: string | null; body?: string | null }
interface RoutineSpec {
  name: string; kind?: string; inputs?: Param[]; outputs?: Param[]; returns?: string; deterministic?: boolean;
  body?: string | null; external?: Ext | null; sql_security?: string | null; comment?: string | null;
}

function ParamsGrid(props: { title: string; value: Param[]; onChange: (v: Param[]) => void; prefix: string }) {
  const v = props.value;
  const upd = (i: number, p: Partial<Param>) => props.onChange(v.map((x, j) => (j === i ? { ...x, ...p } : x)));
  return (
    <div style={{ marginBottom: 12 }}>
      <div className="muted" style={{ marginBottom: 4 }}>{props.title}</div>
      <table className="coltable" style={{ width: "100%" }}>
        <thead><tr className="muted"><td style={{ width: 54 }} /><td style={{ width: "26%" }}>{t("Name")}</td><td>{t("Type (and = default, NOT NULL, TYPE OF …)")}</td><td style={{ width: "26%" }}>{t("Comment")}</td><td /></tr></thead>
        <tbody>
          {v.map((p, i) => (
            <tr key={i}>
              <td className="nowrap">
                <button className="btn icon small" onClick={() => props.onChange(move(v, i, -1))}>↑</button>
                <button className="btn icon small" onClick={() => props.onChange(move(v, i, 1))}>↓</button>
              </td>
              <td><Text value={p.name} upper onChange={(x) => upd(i, { name: x })} /></td>
              <td><Text mono value={p.type} onChange={(x) => upd(i, { type: x })} placeholder="INTEGER" /></td>
              <td><Text value={p.comment} onChange={(x) => upd(i, { comment: x })} /></td>
              <td><button className="btn icon small" title={t("Remove")} onClick={() => props.onChange(v.filter((_, j) => j !== i))}><IconTrash size={13} /></button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn small" style={{ marginTop: 6 }} onClick={() => props.onChange([...v, { name: `${props.prefix}${v.length + 1}`, type: "INTEGER" }])}><IconPlus size={13} /> {t("Add")}</button>
    </div>
  );
}

function BodyEditor(props: { spec: RoutineSpec; set: (p: Partial<RoutineSpec>) => void; deps?: Dependent[] }) {
  const s = props.spec;
  const external = s.kind === "external";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {props.deps && <DependentsNote deps={props.deps} />}
      <div className="seg" style={{ alignSelf: "flex-start" }}>
        <button className={!external ? "on" : ""} onClick={() => props.set({ kind: "psql", external: null, body: s.body ?? "BEGIN\n  \nEND" })}>PSQL</button>
        <button className={external ? "on" : ""} onClick={() => props.set({ kind: "external", external: s.external ?? { name: "module!entry", engine: "UDR" } })}>{t("External (UDR)")}</button>
      </div>
      {external ? (
        <div className="form">
          <Field label={t("External name")} hint="EXTERNAL NAME 'module!entry_point[!info]'"><Text mono value={s.external?.name} onChange={(x) => props.set({ external: { ...s.external, name: x } })} /></Field>
          <Field label={t("Engine")}><Text value={s.external?.engine} upper onChange={(x) => props.set({ external: { ...s.external, engine: x } })} placeholder="UDR" /></Field>
          <Field label={t("Body (AS '…')")} hint={t("Optional text passed to the engine, e.g. for plugins that interpret source")}><Area mono value={s.external?.body} onChange={(x) => props.set({ external: { ...s.external, body: x || null } })} /></Field>
        </div>
      ) : (
        <div className="editor-box tall"><SqlEditor value={s.body ?? ""} onChange={(x) => props.set({ body: x })} /></div>
      )}
    </div>
  );
}

function routineGeneral(s: RoutineSpec, set: (p: Partial<RoutineSpec>) => void, editing: boolean, engine: number, fn: boolean) {
  return (
    <div className="form">
      <Field label={t("Name")} hint={editing ? t("Routines cannot be renamed.") : undefined}><Text value={s.name} upper disabled={editing} onChange={(x) => set({ name: x })} autoFocus /></Field>
      {fn && <>
        <Field label={t("Returns")}><Text mono value={s.returns} onChange={(x) => set({ returns: x })} placeholder="INTEGER" /></Field>
        <span />
        <Check checked={!!s.deterministic} onChange={(x) => set({ deterministic: x })} label="DETERMINISTIC" />
      </>}
      <Field label={t("SQL security")} hint={t("Firebird 4+: run with the privileges of the owner (DEFINER) or of the caller (INVOKER)")}>
        <SecuritySelect value={s.sql_security} engine={engine} onChange={(x) => set({ sql_security: x })} />
      </Field>
      <Field label={t("Comment")}><Area value={s.comment} onChange={(x) => set({ comment: x })} /></Field>
    </div>
  );
}

export function ProcedureDialog(props: { serverId: string; dbId: string; name?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: s, setSpec, original, error } = useSpec<RoutineSpec>(sid, dbid, "procedures", props.name, () => ({
    name: "NEW_PROCEDURE", kind: "psql", inputs: [{ name: "IN_ID", type: "INTEGER" }], outputs: [{ name: "OUT_VALUE", type: "VARCHAR(100)" }],
    body: "BEGIN\n  OUT_VALUE = 'id ' || IN_ID;\n  SUSPEND;\nEND",
  }));
  const deps = useDependents(sid, dbid, "procedures", props.name);
  const set = (p: Partial<RoutineSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const tabs = !s ? [] : [
    { id: "general", title: t("General"), render: () => routineGeneral(s, set, !!props.name, lk?.engine_major ?? 5, false) },
    { id: "params", title: t("Parameters"), render: () => (<>
      <ParamsGrid title={t("Input parameters")} value={s.inputs ?? []} onChange={(v) => set({ inputs: v })} prefix="IN_" />
      <ParamsGrid title={t("Output parameters (RETURNS)")} value={s.outputs ?? []} onChange={(v) => set({ outputs: v })} prefix="OUT_" />
    </>) },
    { id: "definition", title: t("Definition"), render: () => <BodyEditor spec={s} set={set} deps={props.name ? deps : undefined} /> },
  ];
  return <ObjectDialog title={props.name ? t("Procedure {name}", { name: props.name! }) : t("Create procedure")} serverId={sid} dbId={dbid} kind="procedure" otype="procedures"
    spec={s} original={original} loadError={error} tabs={tabs} dependents={deps} selectName={props.name ? undefined : s?.name}
    initialTab={props.name ? "definition" : "general"} extraButtons={<PsqlButton sid={sid} dbid={dbid} otype="procedures" name={props.name} />} />;
}

export function FunctionDialog(props: { serverId: string; dbId: string; name?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: s, setSpec, original, error } = useSpec<RoutineSpec>(sid, dbid, "functions", props.name, () => ({
    name: "NEW_FUNCTION", kind: "psql", inputs: [{ name: "X", type: "INTEGER" }], returns: "INTEGER", deterministic: true,
    body: "BEGIN\n  RETURN X + 1;\nEND",
  }));
  const deps = useDependents(sid, dbid, "functions", props.name);
  const set = (p: Partial<RoutineSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const tabs = !s ? [] : [
    { id: "general", title: t("General"), render: () => routineGeneral(s, set, !!props.name, lk?.engine_major ?? 5, true) },
    { id: "params", title: t("Parameters"), render: () => <ParamsGrid title={t("Arguments")} value={s.inputs ?? []} onChange={(v) => set({ inputs: v })} prefix="ARG" /> },
    { id: "definition", title: t("Definition"), render: () => <BodyEditor spec={s} set={set} deps={props.name ? deps : undefined} /> },
  ];
  return <ObjectDialog title={props.name ? t("Function {name}", { name: props.name! }) : t("Create function")} serverId={sid} dbId={dbid} kind="function" otype="functions"
    spec={s} original={original} loadError={error} tabs={tabs} dependents={deps} selectName={props.name ? undefined : s?.name}
    initialTab={props.name ? "definition" : "general"} extraButtons={<PsqlButton sid={sid} dbid={dbid} otype="functions" name={props.name} />} />;
}

// ------------------------------------------------------------------- trigger
interface TrgSpec extends RoutineSpec { relation?: string | null; event: string; position: number; active: boolean; trigger_kind?: string }
const DB_EVENTS = ["ON CONNECT", "ON DISCONNECT", "ON TRANSACTION START", "ON TRANSACTION COMMIT", "ON TRANSACTION ROLLBACK"];

function triggerKind(trg: TrgSpec): "dml" | "database" | "ddl" {
  if (trg.trigger_kind === "dml" || trg.trigger_kind === "database" || trg.trigger_kind === "ddl") return trg.trigger_kind;
  if (trg.event.startsWith("ON ")) return "database";
  if (trg.relation) return "dml";
  return /^(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)/.test(trg.event) ? "dml" : "ddl";
}

export function TriggerDialog(props: { serverId: string; dbId: string; name?: string; relation?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: trg, setSpec, original, error } = useSpec<TrgSpec>(sid, dbid, "triggers", props.name, () => ({
    name: props.relation ? `${props.relation}_BI` : "NEW_TRIGGER", relation: props.relation ?? null, event: "BEFORE INSERT", position: 0, active: true,
    kind: "psql", body: "BEGIN\n  -- NEW.UPDATED_AT = CURRENT_TIMESTAMP;\nEND",
  }));
  const set = (p: Partial<TrgSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const [kindSel, setKindSel] = useState<"dml" | "database" | "ddl" | null>(null);
  const kind = kindSel ?? (trg ? triggerKind(trg) : "dml");
  const m = /^(BEFORE|AFTER)\s+(.*)$/.exec(trg?.event ?? "");
  const timing = m?.[1] ?? "BEFORE";
  const rest = m?.[2] ?? "";
  const events = rest.split(/\s+OR\s+/).filter((x) => ["INSERT", "UPDATE", "DELETE"].includes(x));
  const setEvents = (tm: string, evs: string[]) => set({ event: `${tm} ${evs.length ? evs.join(" OR ") : "INSERT"}` });
  const editing = !!props.name;
  const tabs = !trg ? [] : [
    {
      id: "general", title: t("General"), render: () => (
        <div className="form">
          <Field label={t("Name")} hint={editing ? t("Triggers cannot be renamed.") : undefined}><Text value={trg.name} upper disabled={editing} onChange={(x) => set({ name: x })} autoFocus /></Field>
          <Field label={t("Trigger type")}>
            <Select value={kind} disabled={editing} onChange={(x) => {
              setKindSel(x as "dml");
              if (x === "dml") set({ event: "BEFORE INSERT", relation: lk?.tables[0] ?? null });
              if (x === "database") set({ event: "ON CONNECT", relation: null });
              if (x === "ddl") set({ event: "AFTER ANY DDL STATEMENT", relation: null });
            }} options={[["dml", t("Table / view (DML)")], ["database", t("Database event")], ["ddl", t("DDL statement")]]} />
          </Field>
          {kind === "dml" && <>
            <Field label={t("Table / view")}><Select value={trg.relation} disabled={editing} onChange={(x) => set({ relation: x })} options={[...(lk?.tables ?? []), ...(lk?.views ?? [])]} empty={t("(choose)")} /></Field>
            <Field label={t("Fires")}>
              <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                <Select value={timing} onChange={(x) => setEvents(x, events)} options={["BEFORE", "AFTER"]} />
                {["INSERT", "UPDATE", "DELETE"].map((e) => (
                  <Check key={e} label={e} checked={events.includes(e)} onChange={(on) => setEvents(timing, on ? ["INSERT", "UPDATE", "DELETE"].filter((x) => x === e || events.includes(x)) : events.filter((x) => x !== e))} />
                ))}
              </div>
            </Field>
          </>}
          {kind === "database" && <Field label={t("Event")}><Select value={trg.event} onChange={(x) => set({ event: x })} options={DB_EVENTS} /></Field>}
          {kind === "ddl" && <Field label={t("Event")} hint={t("e.g. AFTER ANY DDL STATEMENT, BEFORE CREATE TABLE OR ALTER TABLE")}><Text mono value={trg.event} onChange={(x) => set({ event: x.toUpperCase() })} /></Field>}
          <Field label={t("Position")}><input className="text" type="number" style={{ width: 90 }} value={trg.position} onChange={(e) => set({ position: Number(e.target.value) || 0 })} /></Field>
          <span />
          <Check checked={trg.active} onChange={(x) => set({ active: x })} label={t("Active")} />
          <Field label={t("SQL security")}><SecuritySelect value={trg.sql_security} engine={lk?.engine_major ?? 5} onChange={(x) => set({ sql_security: x })} /></Field>
          <Field label={t("Comment")}><Area value={trg.comment} onChange={(x) => set({ comment: x })} /></Field>
        </div>
      ),
    },
    { id: "definition", title: t("Definition"), render: () => <BodyEditor spec={trg} set={set} /> },
  ];
  return <ObjectDialog title={editing ? t("Trigger {name}", { name: props.name! }) : t("Create trigger")} serverId={sid} dbId={dbid} kind="trigger" otype="triggers"
    spec={trg} original={original} loadError={error} tabs={tabs} selectName={editing ? undefined : trg?.name}
    extraButtons={<PsqlButton sid={sid} dbid={dbid} otype="triggers" name={props.name} />} />;
}

// ------------------------------------------------------------------- package
interface PkgSpec { name: string; header: string; body?: string | null; sql_security?: string | null; comment?: string | null }

export function PackageDialog(props: { serverId: string; dbId: string; name?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: p, setSpec, original, error } = useSpec<PkgSpec>(sid, dbid, "packages", props.name, () => ({
    name: "NEW_PACKAGE", header: "BEGIN\n  FUNCTION ADD_ONE (X INTEGER) RETURNS INTEGER;\nEND",
    body: "BEGIN\n  FUNCTION ADD_ONE (X INTEGER) RETURNS INTEGER\n  AS\n  BEGIN\n    RETURN X + 1;\n  END\nEND",
  }));
  const deps = useDependents(sid, dbid, "packages", props.name);
  const set = (x: Partial<PkgSpec>) => setSpec((o) => (o ? { ...o, ...x } : o));
  const tabs = !p ? [] : [
    {
      id: "general", title: t("General"), render: () => (
        <div className="form">
          <Field label={t("Name")} hint={props.name ? t("Packages cannot be renamed.") : undefined}><Text value={p.name} upper disabled={!!props.name} onChange={(x) => set({ name: x })} autoFocus /></Field>
          <Field label={t("SQL security")}><SecuritySelect value={p.sql_security} engine={lk?.engine_major ?? 5} onChange={(x) => set({ sql_security: x })} /></Field>
          <Field label={t("Comment")}><Area value={p.comment} onChange={(x) => set({ comment: x })} /></Field>
        </div>
      ),
    },
    { id: "header", title: t("Header"), render: () => <>{props.name && <DependentsNote deps={deps} />}<div className="editor-box tall"><SqlEditor value={p.header} onChange={(x) => set({ header: x })} /></div></> },
    { id: "body", title: t("Body"), render: () => <div className="editor-box tall"><SqlEditor value={p.body ?? ""} onChange={(x) => set({ body: x })} /></div> },
  ];
  return <ObjectDialog title={props.name ? t("Package {name}", { name: props.name! }) : t("Create package")} serverId={sid} dbId={dbid} kind="package" otype="packages"
    spec={p} original={original} loadError={error} tabs={tabs} dependents={deps} selectName={props.name ? undefined : p?.name}
    extraButtons={<PsqlButton sid={sid} dbid={dbid} otype="packages" name={props.name} />} />;
}

function PsqlButton(props: { sid: string; dbid: string; otype: string; name?: string }) {
  const app = useApp();
  if (!props.name) return null;
  return <button className="btn" onClick={() => { app.showDialog(null); openPsqlEditor(app, props.sid, props.dbid, props.otype, props.name); }}>{t("Edit as source…")}</button>;
}

export function openPsqlEditor(app: ReturnType<typeof useApp>, sid: string, dbid: string, otype: string, name?: string) {
  const ti = typeInfo(otype);
  const title = name ?? t("new {object}", { object: ti ? t(ti.singular).toLowerCase() : t("object") });
  app.openTab({ kind: "psql", title, serverId: sid, dbId: dbid, name, props: { otype, name } });
}

// ---------------------------------------------------------------- PSQL editor
const NAME_RE = /^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER|RECREATE)\s+(PROCEDURE|FUNCTION|TRIGGER|PACKAGE)\s+("(?:[^"]|"")+"|[\w$]+)/i;

export function PsqlEditor(props: { tab: WorkTab; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props.tab;
  const otype: string = props.tab.props?.otype;
  const [name, setName] = useState<string | undefined>(props.tab.props?.name);
  const [text, setText] = useState<string | null>(null);
  const [status, setStatus] = useState<{ kind: "ok" | "err" | "info"; msg: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [dirty, setDirty] = useState(false);
  const deps = useDependents(sid!, dbid!, otype, name);
  const view = useRef<EditorView | null>(null);
  const textRef = useRef("");

  const load = async () => {
    setStatus(null);
    try {
      const sql = name ? (await api.script(sid!, dbid!, otype, name, "create")).sql : CREATE_TEMPLATES[otype] ?? "";
      setText(sql);
      textRef.current = sql;
      setDirty(false);
    } catch (e) {
      setStatus({ kind: "err", msg: errMsg(e) });
      setText("");
    }
  };
  useEffect(() => { load(); }, []);

  const compile = async (test = false) => {
    const sql = textRef.current;
    if (!sql.trim()) return;
    if (name && deps.length && !confirm) {
      setConfirm(true);
      setStatus({ kind: "info", msg: t("{count} object(s) depend on {name}: {names}. ALTER may fail or invalidate them — press Compile again to continue.", {
        count: deps.length, name, names: deps.slice(0, 6).map((d) => d.name).join(", ") + (deps.length > 6 ? ", …" : ""),
      }) });
      return;
    }
    setConfirm(false);
    setBusy(true);
    const v = view.current;
    if (v) clearErrors(v);
    try {
      const r = await api.execute(sid!, dbid!, sql);
      if (r.error) {
        const err = r.error;
        const failed = r.results[err.index];
        const pos = err.position ?? (failed ? { line: failed.line, column: failed.col ?? 1 } : null);
        if (v && pos) showError(v, lineColToOffset(sql, pos.line, pos.column), err.message);
        setStatus({ kind: "err", msg: (pos ? t("Line {line}, column {column}: ", { line: pos.line, column: pos.column }) : "") + err.message });
        return;
      }
      const m = NAME_RE.exec(sql);
      const newName = m ? (m[2].startsWith('"') ? m[2].slice(1, -1).replace(/""/g, '"') : m[2].toUpperCase()) : name;
      setName(newName);
      setDirty(false);
      setStatus({ kind: "ok", msg: t("Compiled successfully at {time}", { time: new Date().toLocaleTimeString() }) });
      app.invalidate(keys.folder(sid!, dbid!, otype));
      app.invalidate(keys.db(sid!, dbid!));
      if (test && newName) {
        const kind = otype === "procedures" ? "execute" : "select";
        const { sql: testSql } = await api.script(sid!, dbid!, otype, newName, kind);
        app.openQueryTool(sid!, dbid!, testSql);
      }
    } catch (e) {
      setStatus({ kind: "err", msg: errMsg(e) });
    } finally {
      setBusy(false);
    }
  };

  const testable = otype === "procedures" || otype === "functions";
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn primary" disabled={busy} onClick={() => compile(false)} title="Ctrl+Enter / F5"><IconPlay /> {confirm ? t("Compile anyway") : t("Compile")}</button>
        <button className="btn" disabled={busy || !testable} onClick={() => compile(true)} title={testable ? t("Compile, then open a Query Tool with an EXECUTE / SELECT script") : t("Only procedures and functions")}>
          {t("Compile and test")}
        </button>
        <span className="sep" />
        <button className="btn" onClick={() => app.openQueryTool(sid!, dbid!, textRef.current)}><IconTerminal /> {t("Query Tool")}</button>
        <button className="btn" disabled={!name} onClick={load} title={t("Reload the source from the database")}><IconRefresh /> {t("Reload")}</button>
        <span className="grow" />
        <span className="muted">{t(typeInfo(otype)?.singular ?? "")} {name ?? t("(new)")}{dirty ? " · " + t("modified") : ""}</span>
      </div>
      {name && deps.length > 0 && <div className="pad" style={{ paddingTop: 8, paddingBottom: 0 }}><DependentsNote deps={deps} /></div>}
      {status && <div className={`psql-status ${status.kind}`}>{status.msg}</div>}
      <div style={{ flex: 1, minHeight: 0 }}>
        {text === null ? <div className="pad muted">{t("Loading…")}</div> : (
          <SqlEditor value={text} onChange={(x) => { textRef.current = x; setDirty(true); if (confirm) setConfirm(false); }}
            onExecute={() => compile(false)} editorRef={(v) => { view.current = v; }} />
        )}
      </div>
    </div>
  );
}

export { quote };

import { useEffect, useState } from "react";
import { useApp } from "../../context";
import { IconPlus, IconTrash } from "../../components/Icons";
import { oapi, type Lookups } from "./api";
import {
  Area, Check, CollationSelect, ColumnsPicker, Field, ObjectDialog, SecuritySelect, Select, Text, TypePicker,
  move, parseType, quote, useLookups, useSpec,
} from "./common";

export interface ColSpec {
  orig_name?: string | null; name: string; data_type?: string | null; domain?: string | null; default?: string | null;
  not_null?: boolean; collation?: string | null; charset?: string | null; identity?: string | null;
  identity_start?: number | string | null; identity_increment?: number | string | null; identity_restart?: number | string | null;
  computed?: string | null; comment?: string | null; domain_not_null?: boolean; domain_default?: string | null;
}
export interface ConSpec {
  orig_name?: string | null; name: string; auto_name?: boolean; type: string; columns: string[]; ref_table?: string | null;
  ref_columns?: string[]; on_update?: string | null; on_delete?: string | null; check?: string | null;
  index_name?: string | null; index_desc?: boolean;
}
export interface IdxSpec {
  orig_name?: string | null; name: string; table?: string; unique?: boolean; descending?: boolean; columns: string[];
  expression?: string | null; condition?: string | null; active?: boolean; comment?: string | null;
  selectivity?: number | null; constraint_name?: string | null; recompute_selectivity?: boolean;
}
export interface TableSpec {
  name: string; kind: string; external_file?: string | null; sql_security?: string | null; comment?: string | null;
  columns: ColSpec[]; constraints: ConSpec[]; indices: IdxSpec[];
  triggers: Array<{ orig_name?: string; name: string; active: boolean; event?: string; position?: number }>;
}

const RULES: Array<[string, string]> = [["", "NO ACTION (default)"], ["CASCADE", "CASCADE"], ["SET NULL", "SET NULL"], ["SET DEFAULT", "SET DEFAULT"]];
const blankTable = (): TableSpec => ({
  name: "NEW_TABLE", kind: "persistent", columns: [
    { name: "ID", data_type: "BIGINT", identity: "BY DEFAULT", not_null: true },
    { name: "NAME", data_type: "VARCHAR(100)", not_null: true },
  ],
  constraints: [{ name: "PK_NEW_TABLE", type: "PRIMARY KEY", columns: ["ID"] }], indices: [], triggers: [],
});

/** Rename a column and every reference to it in constraints and indices. */
function renameColumn(t: TableSpec, i: number, name: string): TableSpec {
  const old = t.columns[i].name;
  const ren = (cols: string[]) => cols.map((c) => (c === old ? name : c));
  return {
    ...t,
    columns: t.columns.map((c, j) => (j === i ? { ...c, name } : c)),
    constraints: t.constraints.map((c) => ({ ...c, columns: ren(c.columns) })),
    indices: t.indices.map((x) => ({ ...x, columns: ren(x.columns) })),
  };
}

// ------------------------------------------------------------------- column
export function ColumnForm(props: { col: ColSpec; onChange: (c: ColSpec) => void; lk: Lookups | null; existing: boolean }) {
  const { col: c, lk } = props;
  const set = (p: Partial<ColSpec>) => props.onChange({ ...c, ...p });
  const mode = c.computed !== null && c.computed !== undefined ? "computed" : c.domain ? "domain" : "type";
  const engine = lk?.engine_major ?? 5;
  const charset = parseType(c.data_type).charset || null;
  return (
    <div className="form">
      <Field label="Name"><Text value={c.name} upper onChange={(v) => set({ name: v })} autoFocus /></Field>
      <Field label="Definition">
        <div className="seg">
          {(["type", "domain", "computed"] as const).map((m) => (
            <button key={m} className={mode === m ? "on" : ""} onClick={() => {
              if (m === "type") set({ domain: null, computed: null, data_type: c.data_type || "VARCHAR(50)" });
              if (m === "domain") set({ domain: c.domain || lk?.domains[0] || "", computed: null });
              if (m === "computed") set({ computed: c.computed || "", domain: null, identity: null, default: null, not_null: false });
            }}>{m === "type" ? "Data type" : m === "domain" ? "Domain" : "Computed"}</button>
          ))}
        </div>
      </Field>
      {mode === "type" && <Field label="Data type"><TypePicker value={c.data_type} lk={lk} onChange={(v) => set({ data_type: v })} /></Field>}
      {mode === "domain" && (
        <Field label="Domain" hint={c.domain_not_null ? "The domain is NOT NULL." : undefined}>
          <Select value={c.domain} onChange={(v) => set({ domain: v })} options={lk?.domains ?? []} empty="(choose)" />
        </Field>
      )}
      {mode === "computed" ? (
        <Field label="Computed by" hint="Expression; Firebird cannot turn a stored column into a computed one in place.">
          <Area mono rows={3} value={c.computed} onChange={(v) => set({ computed: v })} placeholder="(PRICE * QTY)" />
        </Field>
      ) : (
        <>
          <Field label="Collation" hint={props.existing ? "Firebird cannot change the collation of an existing column." : undefined}>
            <CollationSelect value={c.collation} lk={lk} charset={charset} onChange={(v) => set({ collation: v })} />
          </Field>
          <Field label="Identity">
            <Select value={c.identity ?? ""} onChange={(v) => set({ identity: v || null, default: v ? null : c.default })}
              options={[["", "No"], ["BY DEFAULT", "GENERATED BY DEFAULT"], ["ALWAYS", "GENERATED ALWAYS" + (engine < 4 ? " (Firebird 4+)" : "")]]} />
          </Field>
          {c.identity ? (
            <Field label="Identity options">
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span className="muted">start</span>
                <input className="text" style={{ width: 90 }} disabled={props.existing} value={c.identity_start ?? ""} onChange={(e) => set({ identity_start: e.target.value })} />
                <span className="muted">increment</span>
                <input className="text" style={{ width: 70 }} disabled={engine < 4} value={c.identity_increment ?? ""} placeholder="1" onChange={(e) => set({ identity_increment: e.target.value })} />
                {props.existing && <>
                  <span className="muted">restart with</span>
                  <input className="text" style={{ width: 90 }} value={c.identity_restart ?? ""} onChange={(e) => set({ identity_restart: e.target.value })} />
                </>}
              </div>
            </Field>
          ) : (
            <Field label="Default" hint={c.domain_default ? `Domain default: ${c.domain_default}` : "SQL expression, e.g. 0, 'text', CURRENT_TIMESTAMP"}>
              <Text mono value={c.default} onChange={(v) => set({ default: v || null })} />
            </Field>
          )}
          <span />
          <Check checked={!!c.not_null} onChange={(v) => set({ not_null: v })} label="NOT NULL" />
        </>
      )}
      <Field label="Comment"><Area value={c.comment} onChange={(v) => set({ comment: v })} rows={2} /></Field>
    </div>
  );
}

// --------------------------------------------------------------- constraint
export function ConstraintForm(props: { con: ConSpec; onChange: (c: ConSpec) => void; columns: string[]; lk: Lookups | null; sid: string; dbid: string }) {
  const { con: c } = props;
  const set = (p: Partial<ConSpec>) => props.onChange({ ...c, ...p });
  const [ref, setRef] = useState<{ columns: string[]; unique_keys: Array<{ name: string; columns: string[] }> } | null>(null);
  useEffect(() => {
    if (c.type !== "FOREIGN KEY" || !c.ref_table) { setRef(null); return; }
    oapi.relation(props.sid, props.dbid, c.ref_table).then((r) => {
      setRef(r);
      if (!c.ref_columns?.length && r.unique_keys.length) props.onChange({ ...c, ref_columns: r.unique_keys[0].columns });
    }).catch(() => setRef(null));
  }, [c.type, c.ref_table]);
  return (
    <div className="form">
      <Field label="Type">
        <Select value={c.type} onChange={(v) => set({ type: v })} options={["PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK"]} />
      </Field>
      <Field label="Name" hint={c.auto_name ? "System-generated name" : "Leave empty for a system-generated name"}>
        <Text value={c.name} upper onChange={(v) => set({ name: v, auto_name: false })} />
      </Field>
      {c.type === "CHECK" ? (
        <Field label="Condition"><Area mono value={c.check} onChange={(v) => set({ check: v })} placeholder="QTY > 0" /></Field>
      ) : (
        <Field label="Columns"><ColumnsPicker value={c.columns} columns={props.columns} onChange={(v) => set({ columns: v })} /></Field>
      )}
      {c.type === "FOREIGN KEY" && (
        <>
          <Field label="References table">
            <Select value={c.ref_table} onChange={(v) => set({ ref_table: v, ref_columns: [] })} options={props.lk?.tables ?? []} empty="(choose)" />
          </Field>
          <Field label="Referenced columns" hint={ref?.unique_keys.length ? `Keys: ${ref.unique_keys.map((k) => `${k.name} (${k.columns.join(", ")})`).join("; ")}` : undefined}>
            <ColumnsPicker value={c.ref_columns ?? []} columns={ref?.columns ?? []} onChange={(v) => set({ ref_columns: v })} />
          </Field>
          <Field label="On update"><Select value={c.on_update === "RESTRICT" ? "" : c.on_update} onChange={(v) => set({ on_update: v || null })} options={RULES} /></Field>
          <Field label="On delete"><Select value={c.on_delete === "RESTRICT" ? "" : c.on_delete} onChange={(v) => set({ on_delete: v || null })} options={RULES} /></Field>
        </>
      )}
      {c.type !== "CHECK" && (
        <Field label="Index" hint="Optional: name of the index that enforces the constraint (USING INDEX)">
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Text value={c.index_name} upper placeholder="(automatic)" onChange={(v) => set({ index_name: v || null })} />
            <Check checked={!!c.index_desc} onChange={(v) => set({ index_desc: v })} label="DESC" />
          </div>
        </Field>
      )}
    </div>
  );
}

// -------------------------------------------------------------------- index
export function IndexForm(props: { idx: IdxSpec; onChange: (i: IdxSpec) => void; columns: string[]; engine: number;
  existing?: boolean; tables?: string[] }) {
  const { idx: i } = props;
  const set = (p: Partial<IdxSpec>) => props.onChange({ ...i, ...p });
  const expr = i.expression !== null && i.expression !== undefined;
  return (
    <div className="form">
      <Field label="Name"><Text value={i.name} upper onChange={(v) => set({ name: v })} /></Field>
      {props.tables && (
        <Field label="Table"><Select value={i.table} disabled={props.existing} onChange={(v) => set({ table: v, columns: [] })} options={props.tables} empty="(choose)" /></Field>
      )}
      <span />
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
        <Check checked={!!i.unique} onChange={(v) => set({ unique: v })} label="Unique" />
        <Check checked={!!i.descending} onChange={(v) => set({ descending: v })} label="Descending" />
        <Check checked={i.active !== false} onChange={(v) => set({ active: v })} label="Active" />
        <Check checked={expr} onChange={(v) => set({ expression: v ? "" : null })} label="Expression index" />
      </div>
      {expr ? (
        <Field label="Computed by"><Area mono value={i.expression} onChange={(v) => set({ expression: v })} placeholder="UPPER(NAME)" /></Field>
      ) : (
        <Field label="Columns"><ColumnsPicker value={i.columns} columns={props.columns} onChange={(v) => set({ columns: v })} /></Field>
      )}
      <Field label="Partial (WHERE)" hint={props.engine < 5 ? "Partial indices need Firebird 5." : "Only rows matching the condition are indexed."}>
        <Text mono value={i.condition} disabled={props.engine < 5} placeholder={props.engine < 5 ? "Firebird 5+" : "STATUS = 'A'"} onChange={(v) => set({ condition: v || null })} />
      </Field>
      {props.existing && i.selectivity !== undefined && (
        <Field label="Selectivity">
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <span className="mono">{i.selectivity ?? "—"}</span>
            <Check checked={!!i.recompute_selectivity} onChange={(v) => set({ recompute_selectivity: v })} label="Recompute (SET STATISTICS)" />
          </div>
        </Field>
      )}
      <Field label="Comment"><Area value={i.comment} rows={2} onChange={(v) => set({ comment: v })} /></Field>
    </div>
  );
}

// ------------------------------------------------------------ table designer
export function TableDialog(props: { serverId: string; dbId: string; name?: string; initialTab?: string }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: t, setSpec, original, error } = useSpec<TableSpec>(sid, dbid, "tables", props.name, blankTable);
  const [selCol, setSelCol] = useState<number | null>(null);
  const [selCon, setSelCon] = useState<number | null>(null);
  const [selIdx, setSelIdx] = useState<number | null>(null);
  const engine = lk?.engine_major ?? 5;
  const existing = !!props.name;
  const set = (p: Partial<TableSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const cols = t?.columns.map((c) => c.name).filter(Boolean) ?? [];
  const pk = t?.constraints.find((c) => c.type === "PRIMARY KEY");
  const origCols = new Set((original?.columns ?? []).map((c) => c.name));

  const togglePk = (name: string, on: boolean) => {
    if (!t) return;
    let cons = [...t.constraints];
    if (pk) {
      const colsNew = on ? [...pk.columns, name] : pk.columns.filter((c) => c !== name);
      cons = colsNew.length ? cons.map((c) => (c === pk ? { ...c, columns: colsNew } : c)) : cons.filter((c) => c !== pk);
    } else if (on) cons.unshift({ name: `PK_${t.name}`.slice(0, 63), type: "PRIMARY KEY", columns: [name] });
    set({ constraints: cons, columns: on ? t.columns.map((c) => (c.name === name ? { ...c, not_null: true } : c)) : t.columns });
  };

  const tabs = !t ? [] : [
    {
      id: "general", title: "General", render: () => (
        <div className="form">
          <Field label="Name" hint={existing ? "Firebird cannot rename tables." : undefined}>
            <Text value={t.name} upper disabled={existing} onChange={(v) => {
              // generated names (PK_<table>, IX_<table>_1, ...) follow the table name
              const re = new RegExp(`^(PK|UQ|FK|CK|IX)_${t.name.replace(/[$]/g, "\\$&")}(?=_|$)`);
              const fix = (n: string) => n.replace(re, `$1_${v}`);
              set({ name: v, constraints: t.constraints.map((c) => ({ ...c, name: fix(c.name) })), indices: t.indices.map((i) => ({ ...i, name: fix(i.name) })) });
            }} autoFocus />
          </Field>
          <Field label="Table type" hint={existing ? "Fixed once the table exists." : undefined}>
            <Select value={t.kind} disabled={existing} onChange={(v) => set({ kind: v, external_file: v === "persistent" ? t.external_file : null })}
              options={[["persistent", "Persistent"], ["gtt_preserve", "Global temporary — ON COMMIT PRESERVE ROWS"], ["gtt_delete", "Global temporary — ON COMMIT DELETE ROWS"]]} />
          </Field>
          <Field label="External file" hint="EXTERNAL FILE: rows are stored in a fixed-format file on the server (ExternalFileAccess must allow it).">
            <Text mono value={t.external_file} disabled={existing || t.kind !== "persistent"} placeholder="optional, e.g. /data/import.txt" onChange={(v) => set({ external_file: v || null })} />
          </Field>
          <Field label="SQL security" hint="Firebird 4+: privileges checked as the table owner (DEFINER) or the caller (INVOKER) for triggers.">
            <SecuritySelect value={t.sql_security} engine={engine} onChange={(v) => set({ sql_security: v })} />
          </Field>
          <Field label="Comment"><Area value={t.comment} onChange={(v) => set({ comment: v })} /></Field>
        </div>
      ),
    },
    {
      id: "columns", title: `Columns (${t.columns.length})`, render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td /><td>Name</td><td>Type / domain</td><td>Not null</td><td>PK</td><td>Default</td><td>Comment</td><td /></tr></thead>
            <tbody>
              {t.columns.map((c, i) => (
                <tr key={i} className={selCol === i ? "sel" : ""} onClick={() => setSelCol(i)}>
                  <td className="nowrap">
                    <button className="btn icon small" title="Move up" onClick={(e) => { e.stopPropagation(); set({ columns: move(t.columns, i, -1) }); setSelCol(i - 1 >= 0 ? i - 1 : i); }}>↑</button>
                    <button className="btn icon small" title="Move down" onClick={(e) => { e.stopPropagation(); set({ columns: move(t.columns, i, 1) }); setSelCol(i + 1 < t.columns.length ? i + 1 : i); }}>↓</button>
                  </td>
                  <td><input className="text" value={c.name} onChange={(e) => setSpec(renameColumn(t, i, e.target.value.toUpperCase()))} />
                    {c.orig_name && c.orig_name !== c.name && <div className="muted small">was {c.orig_name}</div>}</td>
                  <td style={{ minWidth: 220 }}>
                    {c.computed !== null && c.computed !== undefined ? <span className="mono small">COMPUTED BY {c.computed}</span>
                      : c.domain ? <span className="mono">{c.domain} <span className="muted">(domain)</span></span>
                        : <TypePicker compact value={c.data_type} lk={lk} onChange={(v) => set({ columns: t.columns.map((x, j) => (j === i ? { ...x, data_type: v } : x)) })} />}
                  </td>
                  <td style={{ textAlign: "center" }}><input type="checkbox" checked={!!c.not_null} disabled={c.computed != null}
                    onChange={(e) => set({ columns: t.columns.map((x, j) => (j === i ? { ...x, not_null: e.target.checked } : x)) })} /></td>
                  <td style={{ textAlign: "center" }}><input type="checkbox" checked={!!pk?.columns.includes(c.name)} disabled={c.computed != null}
                    onChange={(e) => togglePk(c.name, e.target.checked)} /></td>
                  <td><input className="text mono" value={c.identity ? `identity (${c.identity.toLowerCase()})` : c.default ?? ""} disabled={!!c.identity || c.computed != null}
                    onChange={(e) => set({ columns: t.columns.map((x, j) => (j === i ? { ...x, default: e.target.value || null } : x)) })} /></td>
                  <td><input className="text" value={c.comment ?? ""} onChange={(e) => set({ columns: t.columns.map((x, j) => (j === i ? { ...x, comment: e.target.value } : x)) })} /></td>
                  <td className="nowrap">
                    <button className="btn small" onClick={(e) => { e.stopPropagation(); setSelCol(i); }}>Edit…</button>
                    <button className="btn icon small" title="Drop column" onClick={(e) => {
                      e.stopPropagation();
                      setSelCol(null);
                      set({ columns: t.columns.filter((_, j) => j !== i), constraints: t.constraints.map((x) => ({ ...x, columns: x.columns.filter((y) => y !== c.name) })).filter((x) => x.type === "CHECK" || x.columns.length) });
                    }}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ margin: "8px 0" }}>
            <button className="btn small" onClick={() => { set({ columns: [...t.columns, { name: `COLUMN_${t.columns.length + 1}`, data_type: "VARCHAR(50)" }] }); setSelCol(t.columns.length); }}><IconPlus size={13} /> Add column</button>
          </div>
          {selCol !== null && t.columns[selCol] && (
            <fieldset className="detail">
              <legend>Column {t.columns[selCol].name}</legend>
              <ColumnForm col={t.columns[selCol]} lk={lk} existing={!!t.columns[selCol].orig_name && origCols.has(t.columns[selCol].orig_name!)}
                onChange={(c) => {
                  const renamed = c.name !== t.columns[selCol].name ? renameColumn(t, selCol, c.name) : t;
                  setSpec({ ...renamed, columns: renamed.columns.map((x, j) => (j === selCol ? c : x)) });
                }} />
            </fieldset>
          )}
        </>
      ),
    },
    {
      id: "constraints", title: `Constraints (${t.constraints.length})`, render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>Name</td><td>Type</td><td>Columns / condition</td><td>References</td><td /></tr></thead>
            <tbody>
              {t.constraints.map((c, i) => (
                <tr key={i} className={selCon === i ? "sel" : ""} onClick={() => setSelCon(i)}>
                  <td>{c.name || <span className="muted">(auto)</span>}</td>
                  <td>{c.type}</td>
                  <td className="mono small">{c.type === "CHECK" ? c.check : c.columns.join(", ")}</td>
                  <td className="small">{c.type === "FOREIGN KEY" ? `${c.ref_table ?? "?"} (${(c.ref_columns ?? []).join(", ")})${c.on_delete && c.on_delete !== "RESTRICT" ? ` · on delete ${c.on_delete.toLowerCase()}` : ""}` : ""}</td>
                  <td className="nowrap">
                    <button className="btn small" onClick={() => setSelCon(i)}>Edit…</button>
                    <button className="btn icon small" title="Drop constraint" onClick={(e) => { e.stopPropagation(); setSelCon(null); set({ constraints: t.constraints.filter((_, j) => j !== i) }); }}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
              {!t.constraints.length && <tr><td colSpan={5} className="muted">No constraints.</td></tr>}
            </tbody>
          </table>
          <div style={{ margin: "8px 0", display: "flex", gap: 6 }}>
            {[["PRIMARY KEY", "PK"], ["UNIQUE", "UQ"], ["FOREIGN KEY", "FK"], ["CHECK", "CK"]].map(([ty, p]) => (
              <button key={ty} className="btn small" disabled={ty === "PRIMARY KEY" && !!pk} onClick={() => {
                const n = t.constraints.filter((c) => c.type === ty).length;
                set({ constraints: [...t.constraints, { name: `${p}_${t.name}${n ? "_" + (n + 1) : ""}`.slice(0, 63), type: ty, columns: [] }] });
                setSelCon(t.constraints.length);
              }}><IconPlus size={13} /> {ty.toLowerCase()}</button>
            ))}
          </div>
          {selCon !== null && t.constraints[selCon] && (
            <fieldset className="detail">
              <legend>Constraint {t.constraints[selCon].name}</legend>
              <ConstraintForm con={t.constraints[selCon]} columns={cols} lk={lk} sid={sid} dbid={dbid}
                onChange={(c) => set({ constraints: t.constraints.map((x, j) => (j === selCon ? c : x)) })} />
            </fieldset>
          )}
        </>
      ),
    },
    {
      id: "indices", title: `Indices (${t.indices.length})`, render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>Name</td><td>Columns / expression</td><td>Unique</td><td>Desc</td><td>Active</td><td /></tr></thead>
            <tbody>
              {t.indices.map((x, i) => (
                <tr key={i} className={selIdx === i ? "sel" : ""} onClick={() => setSelIdx(i)}>
                  <td>{x.name}</td>
                  <td className="mono small">{x.expression ?? x.columns.join(", ")}{x.condition ? ` WHERE ${x.condition}` : ""}</td>
                  <td>{x.unique ? "✓" : ""}</td><td>{x.descending ? "✓" : ""}</td>
                  <td><input type="checkbox" checked={x.active !== false} onChange={(e) => set({ indices: t.indices.map((y, j) => (j === i ? { ...y, active: e.target.checked } : y)) })} /></td>
                  <td className="nowrap">
                    <button className="btn small" onClick={() => setSelIdx(i)}>Edit…</button>
                    <button className="btn icon small" title="Drop index" onClick={(e) => { e.stopPropagation(); setSelIdx(null); set({ indices: t.indices.filter((_, j) => j !== i) }); }}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
              {!t.indices.length && <tr><td colSpan={6} className="muted">No indices (constraint indices are managed by their constraints).</td></tr>}
            </tbody>
          </table>
          <div style={{ margin: "8px 0" }}>
            <button className="btn small" onClick={() => { set({ indices: [...t.indices, { name: `IX_${t.name}_${t.indices.length + 1}`.slice(0, 63), columns: [], active: true }] }); setSelIdx(t.indices.length); }}><IconPlus size={13} /> Add index</button>
          </div>
          {selIdx !== null && t.indices[selIdx] && (
            <fieldset className="detail">
              <legend>Index {t.indices[selIdx].name}</legend>
              <IndexForm idx={t.indices[selIdx]} columns={cols} engine={engine}
                onChange={(x) => set({ indices: t.indices.map((y, j) => (j === selIdx ? x : y)) })} />
            </fieldset>
          )}
        </>
      ),
    },
    ...(existing ? [{
      id: "triggers", title: `Triggers (${t.triggers.length})`, render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>Name</td><td>Event</td><td>Position</td><td>Active</td><td /></tr></thead>
            <tbody>
              {t.triggers.map((x, i) => (
                <tr key={x.name}>
                  <td>{x.name}</td><td>{x.event}</td><td>{x.position}</td>
                  <td><input type="checkbox" checked={x.active} onChange={(e) => set({ triggers: t.triggers.map((y, j) => (j === i ? { ...y, active: e.target.checked } : y)) })} /></td>
                  <td className="nowrap">
                    <button className="btn small" onClick={() => { app.showDialog(null); app.openTab({ kind: "psql", title: x.name, serverId: sid, dbId: dbid, name: x.name, props: { otype: "triggers", name: x.name } }); }}>Edit source</button>
                    <button className="btn icon small" title="Drop trigger" onClick={() => set({ triggers: t.triggers.filter((_, j) => j !== i) })}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
              {!t.triggers.length && <tr><td colSpan={5} className="muted">No triggers.</td></tr>}
            </tbody>
          </table>
          <div className="muted small" style={{ marginTop: 8 }}>
            Activate, deactivate or drop triggers here. "Create trigger…" on the table's menu opens the trigger dialog.
          </div>
        </>
      ),
    }] : []),
  ];

  return (
    <ObjectDialog
      size="xl"
      title={existing ? `Table designer — ${props.name}` : "Create table"}
      serverId={sid} dbId={dbid} kind="table" otype="tables" spec={t} original={original}
      selectName={existing ? undefined : t?.name} loadError={error} tabs={tabs} initialTab={props.initialTab}
    />
  );
}

/** Add or edit one column (same ALTER generator as the designer). */
export function ColumnDialog(props: { serverId: string; dbId: string; table: string; column?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: t, setSpec, original, error } = useSpec<TableSpec>(sid, dbid, "tables", props.table, () => null as unknown as TableSpec);
  const [idx, setIdx] = useState<number | null>(null);
  useEffect(() => {
    if (!t || idx !== null) return;
    if (props.column) setIdx(t.columns.findIndex((c) => c.name === props.column));
    else {
      setSpec({ ...t, columns: [...t.columns, { name: "NEW_COLUMN", data_type: "VARCHAR(50)" }] });
      setIdx(t.columns.length);
    }
  }, [t]);
  const col = t && idx !== null && idx >= 0 ? t.columns[idx] : null;
  const tabs = !t || !col ? [] : [
    {
      id: "general", title: "Column", render: () => (
        <>
          <ColumnForm col={col} lk={lk} existing={!!props.column} onChange={(c) => {
            const renamed = c.name !== col.name ? renameColumn(t, idx!, c.name) : t;
            setSpec({ ...renamed, columns: renamed.columns.map((x, j) => (j === idx ? c : x)) });
          }} />
          <div className="form" style={{ marginTop: 9 }}>
            <Field label="Position" hint="ALTER COLUMN … POSITION">
              <input className="text" type="number" min={1} max={t.columns.length} value={idx! + 1} style={{ width: 80 }}
                onChange={(e) => {
                  const p = Math.max(1, Math.min(t.columns.length, Number(e.target.value) || 1)) - 1;
                  const cs = t.columns.filter((_, j) => j !== idx);
                  cs.splice(p, 0, col);
                  setSpec({ ...t, columns: cs });
                  setIdx(p);
                }} />
            </Field>
          </div>
        </>
      ),
    },
  ];
  return (
    <ObjectDialog title={props.column ? `Column ${props.table}.${props.column}` : `Add column to ${props.table}`}
      serverId={sid} dbId={dbid} kind="table" otype="tables" spec={col ? t : null} original={original}
      loadError={error ?? (idx === -1 ? `Column ${props.column} not found` : null)} tabs={tabs} />
  );
}

/** Add or edit one constraint of a table. */
export function ConstraintDialog(props: { serverId: string; dbId: string; table: string; type?: string; name?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: t, setSpec, original, error } = useSpec<TableSpec>(sid, dbid, "tables", props.table, () => null as unknown as TableSpec);
  const [idx, setIdx] = useState<number | null>(null);
  useEffect(() => {
    if (!t || idx !== null) return;
    if (props.name) setIdx(t.constraints.findIndex((c) => c.name === props.name));
    else {
      const ty = props.type ?? "FOREIGN KEY";
      const p = { "PRIMARY KEY": "PK", UNIQUE: "UQ", "FOREIGN KEY": "FK", CHECK: "CK" }[ty] ?? "C";
      setSpec({ ...t, constraints: [...t.constraints, { name: `${p}_${t.name}_${t.constraints.length + 1}`.slice(0, 63), type: ty, columns: [] }] });
      setIdx(t.constraints.length);
    }
  }, [t]);
  const con = t && idx !== null && idx >= 0 ? t.constraints[idx] : null;
  const tabs = !t || !con ? [] : [{
    id: "general", title: "Constraint", render: () => (
      <ConstraintForm con={con} columns={t.columns.map((c) => c.name)} lk={lk} sid={sid} dbid={dbid}
        onChange={(c) => setSpec({ ...t, constraints: t.constraints.map((x, j) => (j === idx ? c : x)) })} />
    ),
  }];
  return (
    <ObjectDialog title={props.name ? `Constraint ${props.name} on ${props.table}` : `Add constraint to ${props.table}`}
      serverId={sid} dbId={dbid} kind="table" otype="tables" spec={con ? t : null} original={original}
      loadError={error ?? (idx === -1 ? `Constraint ${props.name} not found` : null)} tabs={tabs} />
  );
}

/** Standalone index dialog: create, re-create on change, activate/deactivate, statistics, comment. */
export function IndexDialog(props: { serverId: string; dbId: string; name?: string; table?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec, setSpec, original, error } = useSpec<IdxSpec>(sid, dbid, "indices", props.name,
    () => ({ name: props.table ? `IX_${props.table}_1` : "NEW_INDEX", table: props.table ?? "", columns: [], active: true }));
  const [columns, setColumns] = useState<string[]>([]);
  useEffect(() => {
    if (!spec?.table) return;
    oapi.relation(sid, dbid, spec.table).then((r) => setColumns(r.columns)).catch(() => setColumns([]));
  }, [spec?.table]);
  const owned = !!original?.constraint_name;
  const tabs = !spec ? [] : [{
    id: "general", title: "General", render: () => (
      <>
        {owned && <div className="warn-box">This index enforces constraint {original!.constraint_name}; only activation, statistics and the comment can be changed here. Edit the constraint in the table designer.</div>}
        <IndexForm idx={spec} onChange={(x) => setSpec(owned ? { ...x, name: spec.name, columns: spec.columns, unique: spec.unique, descending: spec.descending, expression: spec.expression } : x)}
          columns={columns} engine={lk?.engine_major ?? 5} existing={!!props.name} tables={lk?.tables} />
        {!!props.name && <div className="muted small" style={{ marginTop: 8 }}>Firebird cannot alter an index definition: changing name, columns or options drops and re-creates it.</div>}
      </>
    ),
  }];
  return (
    <ObjectDialog title={props.name ? `Index ${props.name}` : "Create index"} serverId={sid} dbId={dbid} kind="index" otype="indices"
      spec={spec} original={original} loadError={error} tabs={tabs} selectName={props.name ? undefined : spec?.name} size="wide" />
  );
}

export { quote };

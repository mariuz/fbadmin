import { useEffect, useState } from "react";
import { useApp } from "../../context";
import { IconPlus, IconTrash } from "../../components/Icons";
import { oapi, type Lookups } from "./api";
import {
  Area, Check, CollationSelect, ColumnsPicker, Field, ObjectDialog, SecuritySelect, Select, Text, TypePicker,
  move, parseType, quote, useLookups, useSpec,
} from "./common";
import { N_, t } from "../../i18n";

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

const RULES: Array<[string, string]> = [["", N_("NO ACTION (default)")], ["CASCADE", "CASCADE"], ["SET NULL", "SET NULL"], ["SET DEFAULT", "SET DEFAULT"]];
const blankTable = (): TableSpec => ({
  name: "NEW_TABLE", kind: "persistent", columns: [
    { name: "ID", data_type: "BIGINT", identity: "BY DEFAULT", not_null: true },
    { name: "NAME", data_type: "VARCHAR(100)", not_null: true },
  ],
  constraints: [{ name: "PK_NEW_TABLE", type: "PRIMARY KEY", columns: ["ID"] }], indices: [], triggers: [],
});

/** Rename a column and every reference to it in constraints and indices. */
function renameColumn(tbl: TableSpec, i: number, name: string): TableSpec {
  const old = tbl.columns[i].name;
  const ren = (cols: string[]) => cols.map((c) => (c === old ? name : c));
  return {
    ...tbl,
    columns: tbl.columns.map((c, j) => (j === i ? { ...c, name } : c)),
    constraints: tbl.constraints.map((c) => ({ ...c, columns: ren(c.columns) })),
    indices: tbl.indices.map((x) => ({ ...x, columns: ren(x.columns) })),
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
      <Field label={t("Name")}><Text value={c.name} upper onChange={(v) => set({ name: v })} autoFocus /></Field>
      <Field label={t("Definition")}>
        <div className="seg">
          {(["type", "domain", "computed"] as const).map((m) => (
            <button key={m} className={mode === m ? "on" : ""} onClick={() => {
              if (m === "type") set({ domain: null, computed: null, data_type: c.data_type || "VARCHAR(50)" });
              if (m === "domain") set({ domain: c.domain || lk?.domains[0] || "", computed: null });
              if (m === "computed") set({ computed: c.computed || "", domain: null, identity: null, default: null, not_null: false });
            }}>{m === "type" ? t("Data type") : m === "domain" ? t("Domain") : t("Computed")}</button>
          ))}
        </div>
      </Field>
      {mode === "type" && <Field label={t("Data type")}><TypePicker value={c.data_type} lk={lk} onChange={(v) => set({ data_type: v })} /></Field>}
      {mode === "domain" && (
        <Field label={t("Domain")} hint={c.domain_not_null ? t("The domain is NOT NULL.") : undefined}>
          <Select value={c.domain} onChange={(v) => set({ domain: v })} options={lk?.domains ?? []} empty={t("(choose)")} />
        </Field>
      )}
      {mode === "computed" ? (
        <Field label={t("Computed by")} hint={t("Expression; Firebird cannot turn a stored column into a computed one in place.")}>
          <Area mono rows={3} value={c.computed} onChange={(v) => set({ computed: v })} placeholder="(PRICE * QTY)" />
        </Field>
      ) : (
        <>
          <Field label={t("Collation")} hint={props.existing ? t("Firebird cannot change the collation of an existing column.") : undefined}>
            <CollationSelect value={c.collation} lk={lk} charset={charset} onChange={(v) => set({ collation: v })} />
          </Field>
          <Field label={t("Identity")}>
            <Select value={c.identity ?? ""} onChange={(v) => set({ identity: v || null, default: v ? null : c.default })}
              options={[["", t("No")], ["BY DEFAULT", "GENERATED BY DEFAULT"], ["ALWAYS", "GENERATED ALWAYS" + (engine < 4 ? " " + t("(Firebird 4+)") : "")]]} />
          </Field>
          {c.identity ? (
            <Field label={t("Identity options")}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span className="muted">{t("start")}</span>
                <input className="text" style={{ width: 90 }} disabled={props.existing} value={c.identity_start ?? ""} onChange={(e) => set({ identity_start: e.target.value })} />
                <span className="muted">{t("increment")}</span>
                <input className="text" style={{ width: 70 }} disabled={engine < 4} value={c.identity_increment ?? ""} placeholder="1" onChange={(e) => set({ identity_increment: e.target.value })} />
                {props.existing && <>
                  <span className="muted">{t("restart with")}</span>
                  <input className="text" style={{ width: 90 }} value={c.identity_restart ?? ""} onChange={(e) => set({ identity_restart: e.target.value })} />
                </>}
              </div>
            </Field>
          ) : (
            <Field label={t("Default")} hint={c.domain_default ? t("Domain default: {value}", { value: c.domain_default }) : t("SQL expression, e.g. 0, 'text', CURRENT_TIMESTAMP")}>
              <Text mono value={c.default} onChange={(v) => set({ default: v || null })} />
            </Field>
          )}
          <span />
          <Check checked={!!c.not_null} onChange={(v) => set({ not_null: v })} label="NOT NULL" />
        </>
      )}
      <Field label={t("Comment")}><Area value={c.comment} onChange={(v) => set({ comment: v })} rows={2} /></Field>
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
      <Field label={t("Type")}>
        <Select value={c.type} onChange={(v) => set({ type: v })} options={["PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK"]} />
      </Field>
      <Field label={t("Name")} hint={c.auto_name ? t("System-generated name") : t("Leave empty for a system-generated name")}>
        <Text value={c.name} upper onChange={(v) => set({ name: v, auto_name: false })} />
      </Field>
      {c.type === "CHECK" ? (
        <Field label={t("Condition")}><Area mono value={c.check} onChange={(v) => set({ check: v })} placeholder="QTY > 0" /></Field>
      ) : (
        <Field label={t("Columns")}><ColumnsPicker value={c.columns} columns={props.columns} onChange={(v) => set({ columns: v })} /></Field>
      )}
      {c.type === "FOREIGN KEY" && (
        <>
          <Field label={t("References table")}>
            <Select value={c.ref_table} onChange={(v) => set({ ref_table: v, ref_columns: [] })} options={props.lk?.tables ?? []} empty={t("(choose)")} />
          </Field>
          <Field label={t("Referenced columns")} hint={ref?.unique_keys.length ? t("Keys: {keys}", { keys: ref.unique_keys.map((k) => `${k.name} (${k.columns.join(", ")})`).join("; ") }) : undefined}>
            <ColumnsPicker value={c.ref_columns ?? []} columns={ref?.columns ?? []} onChange={(v) => set({ ref_columns: v })} />
          </Field>
          <Field label={t("On update")}><Select value={c.on_update === "RESTRICT" ? "" : c.on_update} onChange={(v) => set({ on_update: v || null })} options={RULES.map(([v, l]) => [v, t(l)] as [string, string])} /></Field>
          <Field label={t("On delete")}><Select value={c.on_delete === "RESTRICT" ? "" : c.on_delete} onChange={(v) => set({ on_delete: v || null })} options={RULES.map(([v, l]) => [v, t(l)] as [string, string])} /></Field>
        </>
      )}
      {c.type !== "CHECK" && (
        <Field label={t("Index")} hint={t("Optional: name of the index that enforces the constraint (USING INDEX)")}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Text value={c.index_name} upper placeholder={t("(automatic)")} onChange={(v) => set({ index_name: v || null })} />
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
      <Field label={t("Name")}><Text value={i.name} upper onChange={(v) => set({ name: v })} /></Field>
      {props.tables && (
        <Field label={t("Table")}><Select value={i.table} disabled={props.existing} onChange={(v) => set({ table: v, columns: [] })} options={props.tables} empty={t("(choose)")} /></Field>
      )}
      <span />
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
        <Check checked={!!i.unique} onChange={(v) => set({ unique: v })} label={t("Unique")} />
        <Check checked={!!i.descending} onChange={(v) => set({ descending: v })} label={t("Descending")} />
        <Check checked={i.active !== false} onChange={(v) => set({ active: v })} label={t("Active")} />
        <Check checked={expr} onChange={(v) => set({ expression: v ? "" : null })} label={t("Expression index")} />
      </div>
      {expr ? (
        <Field label={t("Computed by")}><Area mono value={i.expression} onChange={(v) => set({ expression: v })} placeholder="UPPER(NAME)" /></Field>
      ) : (
        <Field label={t("Columns")}><ColumnsPicker value={i.columns} columns={props.columns} onChange={(v) => set({ columns: v })} /></Field>
      )}
      <Field label={t("Partial (WHERE)")} hint={props.engine < 5 ? t("Partial indices need Firebird 5.") : t("Only rows matching the condition are indexed.")}>
        <Text mono value={i.condition} disabled={props.engine < 5} placeholder={props.engine < 5 ? t("Firebird 5+") : "STATUS = 'A'"} onChange={(v) => set({ condition: v || null })} />
      </Field>
      {props.existing && i.selectivity !== undefined && (
        <Field label={t("Selectivity")}>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <span className="mono">{i.selectivity ?? "—"}</span>
            <Check checked={!!i.recompute_selectivity} onChange={(v) => set({ recompute_selectivity: v })} label={t("Recompute (SET STATISTICS)")} />
          </div>
        </Field>
      )}
      <Field label={t("Comment")}><Area value={i.comment} rows={2} onChange={(v) => set({ comment: v })} /></Field>
    </div>
  );
}

// ------------------------------------------------------------ table designer
export function TableDialog(props: { serverId: string; dbId: string; name?: string; initialTab?: string }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: tbl, setSpec, original, error } = useSpec<TableSpec>(sid, dbid, "tables", props.name, blankTable);
  const [selCol, setSelCol] = useState<number | null>(null);
  const [selCon, setSelCon] = useState<number | null>(null);
  const [selIdx, setSelIdx] = useState<number | null>(null);
  const engine = lk?.engine_major ?? 5;
  const existing = !!props.name;
  const set = (p: Partial<TableSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const cols = tbl?.columns.map((c) => c.name).filter(Boolean) ?? [];
  const pk = tbl?.constraints.find((c) => c.type === "PRIMARY KEY");
  const origCols = new Set((original?.columns ?? []).map((c) => c.name));

  const togglePk = (name: string, on: boolean) => {
    if (!tbl) return;
    let cons = [...tbl.constraints];
    if (pk) {
      const colsNew = on ? [...pk.columns, name] : pk.columns.filter((c) => c !== name);
      cons = colsNew.length ? cons.map((c) => (c === pk ? { ...c, columns: colsNew } : c)) : cons.filter((c) => c !== pk);
    } else if (on) cons.unshift({ name: `PK_${tbl.name}`.slice(0, 63), type: "PRIMARY KEY", columns: [name] });
    set({ constraints: cons, columns: on ? tbl.columns.map((c) => (c.name === name ? { ...c, not_null: true } : c)) : tbl.columns });
  };

  const tabs = !tbl ? [] : [
    {
      id: "general", title: t("General"), render: () => (
        <div className="form">
          <Field label={t("Name")} hint={existing ? t("Firebird cannot rename tables.") : undefined}>
            <Text value={tbl.name} upper disabled={existing} onChange={(v) => {
              // generated names (PK_<table>, IX_<table>_1, ...) follow the table name
              const re = new RegExp(`^(PK|UQ|FK|CK|IX)_${tbl.name.replace(/[$]/g, "\\$&")}(?=_|$)`);
              const fix = (n: string) => n.replace(re, `$1_${v}`);
              set({ name: v, constraints: tbl.constraints.map((c) => ({ ...c, name: fix(c.name) })), indices: tbl.indices.map((i) => ({ ...i, name: fix(i.name) })) });
            }} autoFocus />
          </Field>
          <Field label={t("Table type")} hint={existing ? t("Fixed once the table exists.") : undefined}>
            <Select value={tbl.kind} disabled={existing} onChange={(v) => set({ kind: v, external_file: v === "persistent" ? tbl.external_file : null })}
              options={[["persistent", t("Persistent")], ["gtt_preserve", t("Global temporary — ON COMMIT PRESERVE ROWS")], ["gtt_delete", t("Global temporary — ON COMMIT DELETE ROWS")]]} />
          </Field>
          <Field label={t("External file")} hint={t("EXTERNAL FILE: rows are stored in a fixed-format file on the server (ExternalFileAccess must allow it).")}>
            <Text mono value={tbl.external_file} disabled={existing || tbl.kind !== "persistent"} placeholder={t("optional, e.g. /data/import.txt")} onChange={(v) => set({ external_file: v || null })} />
          </Field>
          <Field label={t("SQL security")} hint={t("Firebird 4+: privileges checked as the table owner (DEFINER) or the caller (INVOKER) for triggers.")}>
            <SecuritySelect value={tbl.sql_security} engine={engine} onChange={(v) => set({ sql_security: v })} />
          </Field>
          <Field label={t("Comment")}><Area value={tbl.comment} onChange={(v) => set({ comment: v })} /></Field>
        </div>
      ),
    },
    {
      id: "columns", title: t("Columns ({count})", { count: tbl.columns.length }), render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td /><td>{t("Name")}</td><td>{t("Type / domain")}</td><td>{t("Not null")}</td><td>PK</td><td>{t("Default")}</td><td>{t("Comment")}</td><td /></tr></thead>
            <tbody>
              {tbl.columns.map((c, i) => (
                <tr key={i} className={selCol === i ? "sel" : ""} onClick={() => setSelCol(i)}>
                  <td className="nowrap">
                    <button className="btn icon small" title={t("Move up")} onClick={(e) => { e.stopPropagation(); set({ columns: move(tbl.columns, i, -1) }); setSelCol(i - 1 >= 0 ? i - 1 : i); }}>↑</button>
                    <button className="btn icon small" title={t("Move down")} onClick={(e) => { e.stopPropagation(); set({ columns: move(tbl.columns, i, 1) }); setSelCol(i + 1 < tbl.columns.length ? i + 1 : i); }}>↓</button>
                  </td>
                  <td><input className="text" aria-label={t("Name of column {n}", { n: i + 1 })} value={c.name} onChange={(e) => setSpec(renameColumn(tbl, i, e.target.value.toUpperCase()))} />
                    {c.orig_name && c.orig_name !== c.name && <div className="muted small">{t("was {name}", { name: c.orig_name })}</div>}</td>
                  <td style={{ minWidth: 220 }}>
                    {c.computed !== null && c.computed !== undefined ? <span className="mono small">COMPUTED BY {c.computed}</span>
                      : c.domain ? <span className="mono">{c.domain} <span className="muted">{t("(domain)")}</span></span>
                        : <TypePicker compact label={t("Data type of {column}", { column: c.name })} value={c.data_type} lk={lk} onChange={(v) => set({ columns: tbl.columns.map((x, j) => (j === i ? { ...x, data_type: v } : x)) })} />}
                  </td>
                  <td style={{ textAlign: "center" }}><input type="checkbox" aria-label={t("{column} not null", { column: c.name })} checked={!!c.not_null} disabled={c.computed != null}
                    onChange={(e) => set({ columns: tbl.columns.map((x, j) => (j === i ? { ...x, not_null: e.target.checked } : x)) })} /></td>
                  <td style={{ textAlign: "center" }}><input type="checkbox" aria-label={t("{column} in the primary key", { column: c.name })} checked={!!pk?.columns.includes(c.name)} disabled={c.computed != null}
                    onChange={(e) => togglePk(c.name, e.target.checked)} /></td>
                  <td><input className="text mono" aria-label={t("Default of {column}", { column: c.name })} value={c.identity ? `identity (${c.identity.toLowerCase()})` : c.default ?? ""} disabled={!!c.identity || c.computed != null}
                    onChange={(e) => set({ columns: tbl.columns.map((x, j) => (j === i ? { ...x, default: e.target.value || null } : x)) })} /></td>
                  <td><input className="text" aria-label={t("Comment on {column}", { column: c.name })} value={c.comment ?? ""} onChange={(e) => set({ columns: tbl.columns.map((x, j) => (j === i ? { ...x, comment: e.target.value } : x)) })} /></td>
                  <td className="nowrap">
                    <button className="btn small" onClick={(e) => { e.stopPropagation(); setSelCol(i); }}>{t("Edit…")}</button>
                    <button className="btn icon small" title={t("Drop column")} onClick={(e) => {
                      e.stopPropagation();
                      setSelCol(null);
                      set({ columns: tbl.columns.filter((_, j) => j !== i), constraints: tbl.constraints.map((x) => ({ ...x, columns: x.columns.filter((y) => y !== c.name) })).filter((x) => x.type === "CHECK" || x.columns.length) });
                    }}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ margin: "8px 0" }}>
            <button className="btn small" onClick={() => { set({ columns: [...tbl.columns, { name: `COLUMN_${tbl.columns.length + 1}`, data_type: "VARCHAR(50)" }] }); setSelCol(tbl.columns.length); }}><IconPlus size={13} /> {t("Add column")}</button>
          </div>
          {selCol !== null && tbl.columns[selCol] && (
            <fieldset className="detail">
              <legend>{t("Column {name}", { name: tbl.columns[selCol].name })}</legend>
              <ColumnForm col={tbl.columns[selCol]} lk={lk} existing={!!tbl.columns[selCol].orig_name && origCols.has(tbl.columns[selCol].orig_name!)}
                onChange={(c) => {
                  const renamed = c.name !== tbl.columns[selCol].name ? renameColumn(tbl, selCol, c.name) : tbl;
                  setSpec({ ...renamed, columns: renamed.columns.map((x, j) => (j === selCol ? c : x)) });
                }} />
            </fieldset>
          )}
        </>
      ),
    },
    {
      id: "constraints", title: t("Constraints ({count})", { count: tbl.constraints.length }), render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>{t("Name")}</td><td>{t("Type")}</td><td>{t("Columns / condition")}</td><td>{t("References")}</td><td /></tr></thead>
            <tbody>
              {tbl.constraints.map((c, i) => (
                <tr key={i} className={selCon === i ? "sel" : ""} onClick={() => setSelCon(i)}>
                  <td>{c.name || <span className="muted">{t("(auto)")}</span>}</td>
                  <td>{c.type}</td>
                  <td className="mono small">{c.type === "CHECK" ? c.check : c.columns.join(", ")}</td>
                  <td className="small">{c.type === "FOREIGN KEY" ? `${c.ref_table ?? "?"} (${(c.ref_columns ?? []).join(", ")})${c.on_delete && c.on_delete !== "RESTRICT" ? " · " + t("on delete {rule}", { rule: c.on_delete.toLowerCase() }) : ""}` : ""}</td>
                  <td className="nowrap">
                    <button className="btn small" onClick={() => setSelCon(i)}>{t("Edit…")}</button>
                    <button className="btn icon small" title={t("Drop constraint")} onClick={(e) => { e.stopPropagation(); setSelCon(null); set({ constraints: tbl.constraints.filter((_, j) => j !== i) }); }}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
              {!tbl.constraints.length && <tr><td colSpan={5} className="muted">{t("No constraints.")}</td></tr>}
            </tbody>
          </table>
          <div style={{ margin: "8px 0", display: "flex", gap: 6 }}>
            {[["PRIMARY KEY", "PK"], ["UNIQUE", "UQ"], ["FOREIGN KEY", "FK"], ["CHECK", "CK"]].map(([ty, p]) => (
              <button key={ty} className="btn small" disabled={ty === "PRIMARY KEY" && !!pk} onClick={() => {
                const n = tbl.constraints.filter((c) => c.type === ty).length;
                set({ constraints: [...tbl.constraints, { name: `${p}_${tbl.name}${n ? "_" + (n + 1) : ""}`.slice(0, 63), type: ty, columns: [] }] });
                setSelCon(tbl.constraints.length);
              }}><IconPlus size={13} /> {ty.toLowerCase()}</button>
            ))}
          </div>
          {selCon !== null && tbl.constraints[selCon] && (
            <fieldset className="detail">
              <legend>{t("Constraint {name}", { name: tbl.constraints[selCon].name })}</legend>
              <ConstraintForm con={tbl.constraints[selCon]} columns={cols} lk={lk} sid={sid} dbid={dbid}
                onChange={(c) => set({ constraints: tbl.constraints.map((x, j) => (j === selCon ? c : x)) })} />
            </fieldset>
          )}
        </>
      ),
    },
    {
      id: "indices", title: t("Indices ({count})", { count: tbl.indices.length }), render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>{t("Name")}</td><td>{t("Columns / expression")}</td><td>{t("Unique")}</td><td>{t("Desc")}</td><td>{t("Active")}</td><td /></tr></thead>
            <tbody>
              {tbl.indices.map((x, i) => (
                <tr key={i} className={selIdx === i ? "sel" : ""} onClick={() => setSelIdx(i)}>
                  <td>{x.name}</td>
                  <td className="mono small">{x.expression ?? x.columns.join(", ")}{x.condition ? ` WHERE ${x.condition}` : ""}</td>
                  <td>{x.unique ? "✓" : ""}</td><td>{x.descending ? "✓" : ""}</td>
                  <td><input type="checkbox" checked={x.active !== false} onChange={(e) => set({ indices: tbl.indices.map((y, j) => (j === i ? { ...y, active: e.target.checked } : y)) })} /></td>
                  <td className="nowrap">
                    <button className="btn small" onClick={() => setSelIdx(i)}>{t("Edit…")}</button>
                    <button className="btn icon small" title={t("Drop index")} onClick={(e) => { e.stopPropagation(); setSelIdx(null); set({ indices: tbl.indices.filter((_, j) => j !== i) }); }}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
              {!tbl.indices.length && <tr><td colSpan={6} className="muted">{t("No indices (constraint indices are managed by their constraints).")}</td></tr>}
            </tbody>
          </table>
          <div style={{ margin: "8px 0" }}>
            <button className="btn small" onClick={() => { set({ indices: [...tbl.indices, { name: `IX_${tbl.name}_${tbl.indices.length + 1}`.slice(0, 63), columns: [], active: true }] }); setSelIdx(tbl.indices.length); }}><IconPlus size={13} /> {t("Add index")}</button>
          </div>
          {selIdx !== null && tbl.indices[selIdx] && (
            <fieldset className="detail">
              <legend>{t("Index {name}", { name: tbl.indices[selIdx].name })}</legend>
              <IndexForm idx={tbl.indices[selIdx]} columns={cols} engine={engine}
                onChange={(x) => set({ indices: tbl.indices.map((y, j) => (j === selIdx ? x : y)) })} />
            </fieldset>
          )}
        </>
      ),
    },
    ...(existing ? [{
      id: "triggers", title: t("Triggers ({count})", { count: tbl.triggers.length }), render: () => (
        <>
          <table className="coltable designer" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>{t("Name")}</td><td>{t("Event")}</td><td>{t("Position")}</td><td>{t("Active")}</td><td /></tr></thead>
            <tbody>
              {tbl.triggers.map((x, i) => (
                <tr key={x.name}>
                  <td>{x.name}</td><td>{x.event}</td><td>{x.position}</td>
                  <td><input type="checkbox" checked={x.active} onChange={(e) => set({ triggers: tbl.triggers.map((y, j) => (j === i ? { ...y, active: e.target.checked } : y)) })} /></td>
                  <td className="nowrap">
                    <button className="btn small" onClick={() => { app.showDialog(null); app.openTab({ kind: "psql", title: x.name, serverId: sid, dbId: dbid, name: x.name, props: { otype: "triggers", name: x.name } }); }}>{t("Edit source")}</button>
                    <button className="btn icon small" title={t("Drop trigger")} onClick={() => set({ triggers: tbl.triggers.filter((_, j) => j !== i) })}><IconTrash size={13} /></button>
                  </td>
                </tr>
              ))}
              {!tbl.triggers.length && <tr><td colSpan={5} className="muted">{t("No triggers.")}</td></tr>}
            </tbody>
          </table>
          <div className="muted small" style={{ marginTop: 8 }}>
            {t("Activate, deactivate or drop triggers here. \"Create trigger…\" on the table's menu opens the trigger dialog.")}
          </div>
        </>
      ),
    }] : []),
  ];

  return (
    <ObjectDialog
      size="xl"
      title={existing ? t("Table designer — {name}", { name: props.name! }) : t("Create table")}
      serverId={sid} dbId={dbid} kind="table" otype="tables" spec={tbl} original={original}
      selectName={existing ? undefined : tbl?.name} loadError={error} tabs={tabs} initialTab={props.initialTab}
    />
  );
}

/** Add or edit one column (same ALTER generator as the designer). */
export function ColumnDialog(props: { serverId: string; dbId: string; table: string; column?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: tbl, setSpec, original, error } = useSpec<TableSpec>(sid, dbid, "tables", props.table, () => null as unknown as TableSpec);
  const [idx, setIdx] = useState<number | null>(null);
  useEffect(() => {
    if (!tbl || idx !== null) return;
    if (props.column) setIdx(tbl.columns.findIndex((c) => c.name === props.column));
    else {
      setSpec({ ...tbl, columns: [...tbl.columns, { name: "NEW_COLUMN", data_type: "VARCHAR(50)" }] });
      setIdx(tbl.columns.length);
    }
  }, [tbl]);
  const col = tbl && idx !== null && idx >= 0 ? tbl.columns[idx] : null;
  const tabs = !tbl || !col ? [] : [
    {
      id: "general", title: t("Column"), render: () => (
        <>
          <ColumnForm col={col} lk={lk} existing={!!props.column} onChange={(c) => {
            const renamed = c.name !== col.name ? renameColumn(tbl, idx!, c.name) : tbl;
            setSpec({ ...renamed, columns: renamed.columns.map((x, j) => (j === idx ? c : x)) });
          }} />
          <div className="form" style={{ marginTop: 9 }}>
            <Field label={t("Position")} hint="ALTER COLUMN … POSITION">
              <input className="text" type="number" min={1} max={tbl.columns.length} value={idx! + 1} style={{ width: 80 }}
                onChange={(e) => {
                  const p = Math.max(1, Math.min(tbl.columns.length, Number(e.target.value) || 1)) - 1;
                  const cs = tbl.columns.filter((_, j) => j !== idx);
                  cs.splice(p, 0, col);
                  setSpec({ ...tbl, columns: cs });
                  setIdx(p);
                }} />
            </Field>
          </div>
        </>
      ),
    },
  ];
  return (
    <ObjectDialog title={props.column ? t("Column {name}", { name: `${props.table}.${props.column}` }) : t("Add column to {table}", { table: props.table })}
      serverId={sid} dbId={dbid} kind="table" otype="tables" spec={col ? tbl : null} original={original}
      loadError={error ?? (idx === -1 ? t("Column {name} not found", { name: props.column! }) : null)} tabs={tabs} />
  );
}

/** Add or edit one constraint of a table. */
export function ConstraintDialog(props: { serverId: string; dbId: string; table: string; type?: string; name?: string }) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: tbl, setSpec, original, error } = useSpec<TableSpec>(sid, dbid, "tables", props.table, () => null as unknown as TableSpec);
  const [idx, setIdx] = useState<number | null>(null);
  useEffect(() => {
    if (!tbl || idx !== null) return;
    if (props.name) setIdx(tbl.constraints.findIndex((c) => c.name === props.name));
    else {
      const ty = props.type ?? "FOREIGN KEY";
      const p = { "PRIMARY KEY": "PK", UNIQUE: "UQ", "FOREIGN KEY": "FK", CHECK: "CK" }[ty] ?? "C";
      setSpec({ ...tbl, constraints: [...tbl.constraints, { name: `${p}_${tbl.name}_${tbl.constraints.length + 1}`.slice(0, 63), type: ty, columns: [] }] });
      setIdx(tbl.constraints.length);
    }
  }, [tbl]);
  const con = tbl && idx !== null && idx >= 0 ? tbl.constraints[idx] : null;
  const tabs = !tbl || !con ? [] : [{
    id: "general", title: t("Constraint"), render: () => (
      <ConstraintForm con={con} columns={tbl.columns.map((c) => c.name)} lk={lk} sid={sid} dbid={dbid}
        onChange={(c) => setSpec({ ...tbl, constraints: tbl.constraints.map((x, j) => (j === idx ? c : x)) })} />
    ),
  }];
  return (
    <ObjectDialog title={props.name ? t("Constraint {name} on {table}", { name: props.name, table: props.table }) : t("Add constraint to {table}", { table: props.table })}
      serverId={sid} dbId={dbid} kind="table" otype="tables" spec={con ? tbl : null} original={original}
      loadError={error ?? (idx === -1 ? t("Constraint {name} not found", { name: props.name! }) : null)} tabs={tabs} />
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
    id: "general", title: t("General"), render: () => (
      <>
        {owned && <div className="warn-box">{t("This index enforces constraint {name}; only activation, statistics and the comment can be changed here. Edit the constraint in the table designer.", { name: original!.constraint_name! })}</div>}
        <IndexForm idx={spec} onChange={(x) => setSpec(owned ? { ...x, name: spec.name, columns: spec.columns, unique: spec.unique, descending: spec.descending, expression: spec.expression } : x)}
          columns={columns} engine={lk?.engine_major ?? 5} existing={!!props.name} tables={lk?.tables} />
        {!!props.name && <div className="muted small" style={{ marginTop: 8 }}>{t("Firebird cannot alter an index definition: changing name, columns or options drops and re-creates it.")}</div>}
      </>
    ),
  }];
  return (
    <ObjectDialog title={props.name ? t("Index {name}", { name: props.name! }) : t("Create index")} serverId={sid} dbId={dbid} kind="index" otype="indices"
      spec={spec} original={original} loadError={error} tabs={tabs} selectName={props.name ? undefined : spec?.name} size="wide" />
  );
}

export { quote };

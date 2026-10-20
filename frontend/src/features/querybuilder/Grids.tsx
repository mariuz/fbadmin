/** Query builder grids: tables, joins, output columns and criteria (the keyboard-friendly twin of the canvas). */
import { t } from "../../i18n";
import { IconPlus, IconTrash } from "../../components/Icons";
import {
  AGGS, JOIN_TYPES, NO_VALUE_OPS, OPERATORS, newId, type Agg, type JoinType, type Operator, type QbCriterion, type QbField,
  type QbJoin, type QbState, type SortDir,
} from "./model";

type Update = (fn: (s: QbState) => QbState) => void;
const SEP = "\u0001";
const aliasOf = (st: QbState, id: string) => {
  const tb = st.tables.find((x) => x.id === id);
  return tb ? tb.alias || tb.name : "?";
};

/** <select> of every column of every table ("" = free expression). */
function ColumnSelect(props: { st: QbState; table: string; column: string; label: string; onChange: (table: string, column: string) => void; star?: boolean }) {
  const { st } = props;
  return (
    <select className="text" aria-label={props.label} value={props.table ? `${props.table}${SEP}${props.column}` : ""}
      onChange={(e) => {
        const [tb, col] = e.target.value ? e.target.value.split(SEP) : ["", ""];
        props.onChange(tb, col);
      }}>
      <option value="">{t("(expression)")}</option>
      {st.tables.map((tb) => (
        <optgroup key={tb.id} label={`${tb.alias || tb.name} (${tb.name})`}>
          {props.star && <option value={`${tb.id}${SEP}*`}>{`${tb.alias || tb.name}.*`}</option>}
          {tb.columns.map((c) => <option key={c.name} value={`${tb.id}${SEP}${c.name}`}>{`${tb.alias || tb.name}.${c.name}`}</option>)}
        </optgroup>
      ))}
    </select>
  );
}

const aggLabel = (a: Agg) => (a ? a : t("(none)"));

// ------------------------------------------------------------------ tables
export function TablesGrid(props: { st: QbState; update: Update; onRemove: (id: string) => void; onAllColumns: (id: string) => void }) {
  const { st, update } = props;
  if (!st.tables.length) return <div className="qb-empty muted">{t("No tables yet.")}</div>;
  return (
    <table className="qb-grid">
      <thead><tr><th scope="col">{t("Table or view")}</th><th scope="col">{t("Alias")}</th><th scope="col">{t("Columns")}</th><th scope="col"><span className="sr-only">{t("Actions")}</span></th></tr></thead>
      <tbody>
        {st.tables.map((tb) => (
          <tr key={tb.id}>
            <td>{tb.schema ? `${tb.schema}.` : ""}{tb.name} {tb.kind === "view" && <span className="muted">({t("view")})</span>}</td>
            <td><input className="text" aria-label={t("Alias of {table}", { table: tb.name })} value={tb.alias}
              onChange={(e) => update((s) => ({ ...s, tables: s.tables.map((x) => (x.id === tb.id ? { ...x, alias: e.target.value } : x)) }))} /></td>
            <td>{tb.columns.length}</td>
            <td className="qb-actions">
              <button className="btn small" onClick={() => props.onAllColumns(tb.id)}>{t("Add all columns")}</button>
              <button className="btn icon small" aria-label={t("Remove {table}", { table: tb.alias || tb.name })} onClick={() => props.onRemove(tb.id)}><IconTrash size={13} /></button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ------------------------------------------------------------------ joins
export function JoinsGrid(props: { st: QbState; update: Update; active: string | null }) {
  const { st, update } = props;
  const set = (id: string, p: Partial<QbJoin>) => update((s) => ({ ...s, joins: s.joins.map((j) => (j.id === id ? { ...j, ...p } : j)) }));
  const add = () => {
    if (st.tables.length < 2) return;
    update((s) => ({ ...s, joins: [...s.joins, { id: newId("j"), type: "INNER", left: s.tables[0].id, right: s.tables[1].id, conditions: [{ left: "", op: "=", right: "" }], custom: "" }] }));
  };
  return (
    <div>
      {!st.joins.length && <div className="qb-empty muted">{t("No joins. Joins are suggested from foreign keys when you add a table; drag a column onto another table's column, or add one here.")}</div>}
      {st.joins.map((j, n) => {
        const L = st.tables.find((x) => x.id === j.left), R = st.tables.find((x) => x.id === j.right);
        return (
          <fieldset key={j.id} className={"qb-join" + (j.id === props.active ? " active" : "")} id={`qb-join-${j.id}`}>
            <legend>{t("Join {n}", { n: n + 1 })}{j.fk ? <span className="muted"> · {t("foreign key {name}", { name: j.fk })}</span> : null}</legend>
            <div className="qb-join-head">
              <select className="text" aria-label={t("Left table of join {n}", { n: n + 1 })} value={j.left} onChange={(e) => set(j.id, { left: e.target.value })}>
                {st.tables.map((x) => <option key={x.id} value={x.id}>{x.alias || x.name}</option>)}
              </select>
              <select className="text" aria-label={t("Type of join {n}", { n: n + 1 })} value={j.type} onChange={(e) => set(j.id, { type: e.target.value as JoinType })}>
                {JOIN_TYPES.map((k) => <option key={k} value={k}>{k} JOIN</option>)}
              </select>
              <select className="text" aria-label={t("Right table of join {n}", { n: n + 1 })} value={j.right} onChange={(e) => set(j.id, { right: e.target.value })}>
                {st.tables.map((x) => <option key={x.id} value={x.id}>{x.alias || x.name}</option>)}
              </select>
              <span className="grow" />
              <button className="btn icon small" aria-label={t("Remove join {n}", { n: n + 1 })} onClick={() => update((s) => ({ ...s, joins: s.joins.filter((x) => x.id !== j.id) }))}><IconTrash size={13} /></button>
            </div>
            {!j.custom.trim() && j.conditions.map((c, k) => (
              <div key={k} className="qb-join-cond">
                <span className="muted">{k ? "AND" : "ON"}</span>
                <select className="text" aria-label={t("Join {n}, condition {k}: left column", { n: n + 1, k: k + 1 })} value={c.left}
                  onChange={(e) => set(j.id, { conditions: j.conditions.map((x, i) => (i === k ? { ...x, left: e.target.value } : x)) })}>
                  <option value="">—</option>
                  {L?.columns.map((col) => <option key={col.name} value={col.name}>{`${aliasOf(st, j.left)}.${col.name}`}</option>)}
                </select>
                <select className="text" aria-label={t("Join {n}, condition {k}: operator", { n: n + 1, k: k + 1 })} value={c.op}
                  onChange={(e) => set(j.id, { conditions: j.conditions.map((x, i) => (i === k ? { ...x, op: e.target.value } : x)) })}>
                  {["=", "<>", "<", "<=", ">", ">="].map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
                <select className="text" aria-label={t("Join {n}, condition {k}: right column", { n: n + 1, k: k + 1 })} value={c.right}
                  onChange={(e) => set(j.id, { conditions: j.conditions.map((x, i) => (i === k ? { ...x, right: e.target.value } : x)) })}>
                  <option value="">—</option>
                  {R?.columns.map((col) => <option key={col.name} value={col.name}>{`${aliasOf(st, j.right)}.${col.name}`}</option>)}
                </select>
                <button className="btn icon small" aria-label={t("Remove condition {k} of join {n}", { n: n + 1, k: k + 1 })}
                  onClick={() => set(j.id, { conditions: j.conditions.filter((_, i) => i !== k) })}><IconTrash size={12} /></button>
              </div>
            ))}
            <div className="qb-join-cond">
              {!j.custom.trim() && <button className="btn small" onClick={() => set(j.id, { conditions: [...j.conditions, { left: "", op: "=", right: "" }] })}><IconPlus size={12} /> {t("Condition")}</button>}
              <label className="qb-custom">{t("Custom ON (replaces the conditions)")}
                <input className="text mono" value={j.custom} placeholder={t("e.g. {example}", { example: `${aliasOf(st, j.left)}.ID = ${aliasOf(st, j.right)}.ID AND …` })}
                  onChange={(e) => set(j.id, { custom: e.target.value })} />
              </label>
            </div>
          </fieldset>
        );
      })}
      <button className="btn small" disabled={st.tables.length < 2} onClick={add}><IconPlus size={12} /> {t("Add join")}</button>
    </div>
  );
}

// ------------------------------------------------------------------ output columns
export function FieldsGrid(props: { st: QbState; update: Update }) {
  const { st, update } = props;
  const set = (id: string, p: Partial<QbField>) => update((s) => ({ ...s, fields: s.fields.map((f) => (f.id === id ? { ...f, ...p } : f)) }));
  const move = (i: number, d: number) => update((s) => {
    const f = [...s.fields];
    const j = i + d;
    if (j < 0 || j >= f.length) return s;
    [f[i], f[j]] = [f[j], f[i]];
    return { ...s, fields: f };
  });
  const add = () => update((s) => ({
    ...s, fields: [...s.fields, {
      id: newId("f"), table: s.tables[0]?.id ?? "", column: s.tables[0]?.columns[0]?.name ?? "", expr: "", agg: "", alias: "",
      output: true, sort: "", sortOrder: 1 + Math.max(0, ...s.fields.map((x) => x.sortOrder)),
    }],
  }));
  return (
    <div>
      {st.fields.length > 0 && (
        <table className="qb-grid">
          <thead><tr>
            <th scope="col">{t("Output")}</th><th scope="col">{t("Column or expression")}</th><th scope="col">{t("Aggregate")}</th>
            <th scope="col">{t("Alias")}</th><th scope="col">{t("Sort")}</th><th scope="col">{t("Sort order")}</th><th scope="col"><span className="sr-only">{t("Actions")}</span></th>
          </tr></thead>
          <tbody>
            {st.fields.map((f, i) => (
              <tr key={f.id}>
                <td><input type="checkbox" aria-label={t("Show column {n} in the output", { n: i + 1 })} checked={f.output} onChange={(e) => set(f.id, { output: e.target.checked })} /></td>
                <td className="qb-colcell">
                  <ColumnSelect st={st} table={f.table} column={f.column} star label={t("Column {n}", { n: i + 1 })} onChange={(table, column) => set(f.id, { table, column })} />
                  {!f.table && <input className="text mono" aria-label={t("Expression of column {n}", { n: i + 1 })} placeholder={t("SQL expression, e.g. {example}", { example: "UPPER(C.NAME)" })} value={f.expr} onChange={(e) => set(f.id, { expr: e.target.value })} />}
                </td>
                <td><select className="text" aria-label={t("Aggregate of column {n}", { n: i + 1 })} value={f.agg} onChange={(e) => set(f.id, { agg: e.target.value as Agg })}>
                  {AGGS.map((a) => <option key={a} value={a}>{aggLabel(a)}</option>)}
                </select></td>
                <td><input className="text" aria-label={t("Alias of column {n}", { n: i + 1 })} value={f.alias} onChange={(e) => set(f.id, { alias: e.target.value })} /></td>
                <td><select className="text" aria-label={t("Sort of column {n}", { n: i + 1 })} value={f.sort} onChange={(e) => set(f.id, { sort: e.target.value as SortDir })}>
                  <option value="">{t("(none)")}</option><option value="ASC">{t("Ascending")}</option><option value="DESC">{t("Descending")}</option>
                </select></td>
                <td><input className="text" type="number" min={1} style={{ width: 60 }} aria-label={t("Sort order of column {n}", { n: i + 1 })} value={f.sortOrder} disabled={!f.sort}
                  onChange={(e) => set(f.id, { sortOrder: Number(e.target.value) || 0 })} /></td>
                <td className="qb-actions">
                  <button className="btn icon small" aria-label={t("Move column {n} up", { n: i + 1 })} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                  <button className="btn icon small" aria-label={t("Move column {n} down", { n: i + 1 })} disabled={i === st.fields.length - 1} onClick={() => move(i, 1)}>↓</button>
                  <button className="btn icon small" aria-label={t("Remove column {n}", { n: i + 1 })} onClick={() => update((s) => ({ ...s, fields: s.fields.filter((x) => x.id !== f.id) }))}><IconTrash size={13} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!st.fields.length && <div className="qb-empty muted">{t("No columns picked: the query selects all columns. Tick columns in the table boxes or add them here.")}</div>}
      <button className="btn small" onClick={add}><IconPlus size={12} /> {t("Add column")}</button>
      {st.fields.some((f) => f.output && f.agg) && <p className="muted qb-note">{t("Aggregates are used: GROUP BY is added for the other output columns.")}</p>}
    </div>
  );
}

// ------------------------------------------------------------------ criteria
export function CriteriaGrid(props: { st: QbState; update: Update }) {
  const { st, update } = props;
  const set = (id: string, p: Partial<QbCriterion>) => update((s) => ({ ...s, criteria: s.criteria.map((c) => (c.id === id ? { ...c, ...p } : c)) }));
  const add = () => update((s) => ({
    ...s, criteria: [...s.criteria, {
      id: newId("c"), table: s.tables[0]?.id ?? "", column: s.tables[0]?.columns[0]?.name ?? "", expr: "", agg: "", op: "=", value: "", value2: "", connector: "AND",
    }],
  }));
  return (
    <div>
      {st.criteria.length > 0 && (
        <table className="qb-grid">
          <thead><tr>
            <th scope="col"><span className="sr-only">{t("AND / OR")}</span></th><th scope="col">{t("Column or expression")}</th><th scope="col">{t("Aggregate (HAVING)")}</th>
            <th scope="col">{t("Operator")}</th><th scope="col">{t("Value")}</th><th scope="col"><span className="sr-only">{t("Actions")}</span></th>
          </tr></thead>
          <tbody>
            {st.criteria.map((c, i) => (
              <tr key={c.id}>
                <td>{i === 0 ? <span className="muted">{c.agg ? "HAVING" : "WHERE"}</span> : (
                  <select className="text" aria-label={t("Combine condition {n} with", { n: i + 1 })} value={c.connector} onChange={(e) => set(c.id, { connector: e.target.value as "AND" | "OR" })}>
                    <option value="AND">AND</option><option value="OR">OR</option>
                  </select>
                )}</td>
                <td className="qb-colcell">
                  <ColumnSelect st={st} table={c.table} column={c.column} label={t("Condition {n}: column", { n: i + 1 })} onChange={(table, column) => set(c.id, { table, column })} />
                  {!c.table && <input className="text mono" aria-label={t("Condition {n}: expression", { n: i + 1 })} value={c.expr} placeholder={t("SQL expression")} onChange={(e) => set(c.id, { expr: e.target.value })} />}
                </td>
                <td><select className="text" aria-label={t("Condition {n}: aggregate", { n: i + 1 })} value={c.agg} onChange={(e) => set(c.id, { agg: e.target.value as Agg })}>
                  {AGGS.map((a) => <option key={a} value={a}>{aggLabel(a)}</option>)}
                </select></td>
                <td><select className="text" aria-label={t("Condition {n}: operator", { n: i + 1 })} value={c.op} onChange={(e) => set(c.id, { op: e.target.value as Operator })}>
                  {OPERATORS.map((o) => <option key={o} value={o}>{o}</option>)}
                </select></td>
                <td className="qb-valcell">
                  {!NO_VALUE_OPS.includes(c.op) && (
                    <input className="text mono" aria-label={t("Condition {n}: value", { n: i + 1 })} value={c.value}
                      placeholder={c.op === "IN" || c.op === "NOT IN" ? t("a, b, c") : t("value or :param")} onChange={(e) => set(c.id, { value: e.target.value })} />
                  )}
                  {c.op === "BETWEEN" && <>
                    <span className="muted">AND</span>
                    <input className="text mono" aria-label={t("Condition {n}: second value", { n: i + 1 })} value={c.value2} onChange={(e) => set(c.id, { value2: e.target.value })} />
                  </>}
                </td>
                <td className="qb-actions"><button className="btn icon small" aria-label={t("Remove condition {n}", { n: i + 1 })} onClick={() => update((s) => ({ ...s, criteria: s.criteria.filter((x) => x.id !== c.id) }))}><IconTrash size={13} /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button className="btn small" onClick={add} disabled={!st.tables.length}><IconPlus size={12} /> {t("Add condition")}</button>
      <p className="muted qb-note">{t("Values: numbers, :name parameters, NULL, TRUE/FALSE and CURRENT_DATE-style keywords are used as written; other text is quoted as a string. Start with = to write a raw SQL expression (e.g. =CURRENT_DATE - 7). AND binds before OR.")}</p>
    </div>
  );
}

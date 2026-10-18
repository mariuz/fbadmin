import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import { IconPlus, IconRefresh } from "../../components/Icons";
import { typeInfo, type Selection } from "../../types";
import { oapi, type Grant } from "./api";
import { SqlTab, useDdl } from "./common";
import { N_, t } from "../../i18n";

const ALL_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "EXECUTE", "USAGE", "MEMBER"];
const SHORT: Record<string, string> = {
  SELECT: N_("Select"), INSERT: N_("Insert"), UPDATE: N_("Update"), DELETE: N_("Delete"), REFERENCES: N_("References"),
  EXECUTE: N_("Execute"), USAGE: N_("Usage"), MEMBER: N_("Member"),
};

/** Render a translated sentence with {name} shown in bold. */
function withBold(text: string, bold: ReactNode) {
  const [a, b = ""] = text.split("{name}");
  return <>{a}<b>{bold}</b>{b}</>;
}
export const APPLICABLE: Record<string, string[]> = {
  tables: ["SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES"],
  views: ["SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES"],
  procedures: ["EXECUTE"], functions: ["EXECUTE"], packages: ["EXECUTE"],
  sequences: ["USAGE"], exceptions: ["USAGE"], roles: ["MEMBER"],
};
const COLUMN_PRIVS = new Set(["UPDATE", "REFERENCES"]);
const GRANTEE_TYPES = ["USER", "ROLE", "PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER", "VIEW"];
const GRANTEE_LIST: Record<string, string> = { USER: "users", ROLE: "roles", PROCEDURE: "procedures", FUNCTION: "functions", PACKAGE: "packages", TRIGGER: "triggers", VIEW: "views" };

const typeLabel = (type: string) => {
  const ti = typeInfo(type);
  return ti ? t(ti.singular) : type;
};

export const privilegesApplies = (sel: Selection | null) =>
  !!sel && sel.kind === "object" && (sel.otype! in APPLICABLE || sel.otype === "users");

interface RowKey { gtype: string; grantee: string; otype: string; object: string }

export function PrivilegesPanel(props: { sel: Selection }) {
  const { sel } = props;
  const [mode, setMode] = useState<"object" | "grantee">(sel.otype === "users" ? "grantee" : "object");
  useEffect(() => setMode(sel.otype === "users" ? "grantee" : "object"), [sel.otype, sel.name]);
  return (
    <div className="pad privs">
      <h2 style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {t("Privileges · {name}", { name: sel.name ?? "" })}
        {sel.otype === "roles" && (
          <span className="seg" style={{ fontSize: 12, fontWeight: 400 }}>
            <button className={mode === "object" ? "on" : ""} onClick={() => setMode("object")}>{t("Members of the role")}</button>
            <button className={mode === "grantee" ? "on" : ""} onClick={() => setMode("grantee")}>{t("Privileges held by the role")}</button>
          </span>
        )}
      </h2>
      <PrivilegeEditor key={`${sel.serverId}:${sel.dbId}:${sel.otype}:${sel.name}:${mode}`} sel={sel} mode={mode} />
    </div>
  );
}

function PrivilegeEditor(props: { sel: Selection; mode: "object" | "grantee" }) {
  const app = useApp();
  const { sel, mode } = props;
  const sid = sel.serverId, dbid = sel.dbId!, name = sel.name!;
  const [orig, setOrig] = useState<Grant[] | null>(null);
  const [grants, setGrants] = useState<Grant[]>([]);
  const [readonly, setReadonly] = useState<Grant[]>([]);
  const [columns, setColumns] = useState<string[]>([]);
  const [extraRows, setExtraRows] = useState<RowKey[]>([]);
  const [grantees, setGrantees] = useState<Record<string, string[]>>({});
  const [objects, setObjects] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSql, setShowSql] = useState(false);
  const [colPick, setColPick] = useState<{ row: RowKey; priv: string; cols: string[] } | null>(null);
  const [add, setAdd] = useState({ type: mode === "object" ? "USER" : "tables", name: "" });
  const selfType = sel.otype === "roles" ? "ROLE" : "USER";

  const load = async () => {
    setError(null);
    try {
      const r = await oapi.privileges(sid, dbid, mode === "object" ? { object: name } : { grantee: name });
      const editable = r.grants.filter((g) => g.object_type in APPLICABLE && (mode === "object" || g.grantee_type === selfType));
      setReadonly(r.grants.filter((g) => !editable.includes(g)));
      setOrig(editable);
      setGrants(editable);
      setColumns(r.columns ?? []);
      setExtraRows([]);
    } catch (e) {
      setError(errMsg(e));
    }
  };
  useEffect(() => {
    load();
    oapi.grantees(sid, dbid).then(setGrantees).catch(() => undefined);
    if (mode === "grantee") oapi.grantable(sid, dbid).then(setObjects).catch(() => undefined);
  }, []);

  const objType = sel.otype!;
  const privs = mode === "object" ? APPLICABLE[objType] ?? [] : ALL_PRIVS;
  const rowOf = (g: Grant): RowKey => ({ gtype: g.grantee_type, grantee: g.grantee, otype: g.object_type, object: g.object });
  const rkey = (r: RowKey) => mode === "object" ? `${r.gtype}:${r.grantee}` : `${r.otype}:${r.object}`;
  const rows = useMemo(() => {
    const m = new Map<string, RowKey>();
    for (const g of [...(orig ?? []), ...grants]) m.set(rkey(rowOf(g)), rowOf(g));
    for (const r of extraRows) m.set(rkey(r), r);
    return Array.from(m.values()).sort((a, b) => rkey(a).localeCompare(rkey(b)));
  }, [orig, grants, extraRows]);
  const match = (g: Grant, r: RowKey, priv: string) =>
    g.grantee === r.grantee && g.grantee_type === r.gtype && g.object === r.object && g.object_type === r.otype && g.privilege === priv;
  const cell = (r: RowKey, priv: string) => grants.filter((g) => match(g, r, priv));
  const toggle = (r: RowKey, priv: string, on: boolean) => {
    const rest = grants.filter((g) => !(match(g, r, priv) && !g.column));
    setGrants(on ? [...rest, { grantee: r.grantee, grantee_type: r.gtype, object: r.object, object_type: r.otype, privilege: priv, column: null,
      grant_option: cell(r, priv).some((g) => g.grant_option) }] : rest);
  };
  const setOpt = (r: RowKey, priv: string, on: boolean) => setGrants(grants.map((g) => (match(g, r, priv) ? { ...g, grant_option: on } : g)));
  const setCols = (r: RowKey, priv: string, cols: string[]) => {
    const opt = cell(r, priv).some((g) => g.grant_option);
    const rest = grants.filter((g) => !(match(g, r, priv) && g.column));
    setGrants([...rest, ...cols.map((c) => ({ grantee: r.grantee, grantee_type: r.gtype, object: r.object, object_type: r.otype, privilege: priv, column: c, grant_option: opt }))]);
  };
  const openCols = async (r: RowKey, priv: string) => {
    let cols = columns;
    if (mode === "grantee") {
      try { cols = (await oapi.relation(sid, dbid, r.object)).columns; } catch { cols = []; }
    }
    setColPick({ row: r, priv, cols });
  };

  const ddl = useDdl(sid, dbid, "grants", showSql ? { grants } : null, { grants: orig ?? [] });
  const changed = JSON.stringify(orig) !== JSON.stringify(grants);

  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await oapi.ddl(sid, dbid, "grants", { grants }, { grants: orig ?? [] });
      if (s.sql.trim()) {
        const r = await api.execute(sid, dbid, s.sql);
        if (r.error) throw new Error(r.error.message);
        app.toast(t("{count} GRANT/REVOKE statement(s) applied", { count: s.statements.length }));
      }
      setShowSql(false);
      await load();
    } catch (e) {
      setError(errMsg(e));
      setShowSql(true);
    } finally {
      setBusy(false);
    }
  };

  const addRow = () => {
    const n = add.name.trim();
    if (!n) return;
    const r: RowKey = mode === "object"
      ? { gtype: n.toUpperCase() === "PUBLIC" ? "USER" : add.type, grantee: n, otype: objType, object: name }
      : { gtype: selfType, grantee: name, otype: add.type, object: n };
    setExtraRows([...extraRows, r]);
    setAdd({ ...add, name: "" });
  };

  if (error && !orig) return <div className="err">{error}</div>;
  if (!orig) return <div className="muted">{t("Loading…")}</div>;
  const listId = `privs-list-${mode}`;
  const addOptions = mode === "object" ? grantees[GRANTEE_LIST[add.type]] ?? [] : objects[add.type] ?? [];
  return (
    <>
      <div className="muted small" style={{ marginBottom: 8 }}>
        {mode === "object"
          ? withBold(t("Who has which privilege on {object} {name}. \"G\" = WITH {option} OPTION. Column privileges: use “cols”.", {
            object: typeLabel(objType).toLowerCase(), option: objType === "roles" ? "ADMIN" : "GRANT",
          }), name)
          : withBold(t("What {name} may do. Cells that do not apply to an object type are disabled."), name)}
      </div>
      <div className="grid-wrap" style={{ maxHeight: "none" }}>
        <table className="grid privs-grid">
          <thead><tr>
            <th>{mode === "object" ? t("Grantee") : t("Object")}</th>
            <th>{mode === "object" ? t("Type") : t("Kind")}</th>
            {privs.map((p) => <th key={p} style={{ textAlign: "center" }}>{t(SHORT[p])}</th>)}
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={rkey(r)}>
                <td className="mono">{mode === "object" ? r.grantee : r.object}</td>
                <td className="muted">{mode === "object" ? r.gtype.toLowerCase() : typeLabel(r.otype).toLowerCase()}</td>
                {privs.map((p) => {
                  const applicable = (APPLICABLE[r.otype] ?? []).includes(p);
                  const cs = cell(r, p);
                  const whole = cs.some((g) => !g.column);
                  const colNames = cs.filter((g) => g.column).map((g) => g.column!);
                  const opt = cs.some((g) => g.grant_option);
                  return (
                    <td key={p} className="pcell" style={{ textAlign: "center" }}>
                      {applicable && (
                        <span className="pcell-inner">
                          <input type="checkbox" title={p + (whole ? " " + t("(granted)") : "")} checked={whole} onChange={(e) => toggle(r, p, e.target.checked)} />
                          <label className="gopt" title={`WITH ${p === "MEMBER" ? "ADMIN" : "GRANT"} OPTION`}>
                            <input type="checkbox" disabled={!cs.length} checked={opt} onChange={(e) => setOpt(r, p, e.target.checked)} />G
                          </label>
                          {COLUMN_PRIVS.has(p) && (r.otype === "tables" || r.otype === "views") && (
                            <button className="btn small cols" title={t("Column-level privilege")} onClick={() => openCols(r, p)}>
                              {colNames.length ? `(${colNames.join(", ")})` : t("cols")}
                            </button>
                          )}
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={privs.length + 2} className="muted">{t("No privileges granted.")}</td></tr>}
          </tbody>
        </table>
      </div>
      {colPick && (
        <div className="col-pick">
          <b>{colPick.priv}</b>{" " + t("on columns of {object} for {grantee}:", { object: colPick.row.object, grantee: colPick.row.grantee })}
          <div className="sysprivs" style={{ marginTop: 6 }}>
            {colPick.cols.map((c) => {
              const cur = cell(colPick.row, colPick.priv).filter((g) => g.column).map((g) => g.column!);
              return (
                <label key={c} className="check"><input type="checkbox" checked={cur.includes(c)}
                  onChange={(e) => setCols(colPick.row, colPick.priv, e.target.checked ? [...cur, c] : cur.filter((x) => x !== c))} /> {c}</label>
              );
            })}
          </div>
          <button className="btn small" style={{ marginTop: 6 }} onClick={() => setColPick(null)}>{t("Done")}</button>
        </div>
      )}
      <div className="toolbar" style={{ border: 0, background: "none", padding: "10px 0" }}>
        <select className="text" value={add.type} onChange={(e) => setAdd({ type: e.target.value, name: "" })}>
          {(mode === "object" ? GRANTEE_TYPES : Object.keys(APPLICABLE)).map((ty) => <option key={ty} value={ty}>{mode === "object" ? ty.toLowerCase() : typeLabel(ty)}</option>)}
        </select>
        <input className="text" list={listId} value={add.name} placeholder={mode === "object" ? t("grantee name or PUBLIC") : t("object name")}
          onChange={(e) => setAdd({ ...add, name: e.target.value.toUpperCase() })} onKeyDown={(e) => e.key === "Enter" && addRow()} />
        <datalist id={listId}>{addOptions.map((o) => <option key={o} value={o} />)}</datalist>
        <button className="btn small" onClick={addRow}><IconPlus size={13} /> {mode === "object" ? t("Add grantee") : t("Add object")}</button>
        <span className="grow" />
        <button className="btn" onClick={load}><IconRefresh /> {t("Reload")}</button>
        <button className="btn" disabled={!changed} onClick={() => { setGrants(orig); setExtraRows([]); }}>{t("Revert")}</button>
        <button className="btn" disabled={!changed} onClick={() => setShowSql(!showSql)}>{showSql ? t("Hide SQL") : t("Preview SQL")}</button>
        <button className="btn primary" disabled={!changed || busy} onClick={apply}>{t("Apply")}</button>
      </div>
      {error && <div className="err" style={{ whiteSpace: "pre-wrap", marginBottom: 8 }}>{error}</div>}
      {showSql && <SqlTab ddl={ddl} />}
      {readonly.length > 0 && (
        <>
          <h3>{t("Other privileges (read-only)")}</h3>
          <div className="grid-wrap">
            <table className="grid"><thead><tr><th>{t("Grantee")}</th><th>{t("Privilege")}</th><th>{t("Object")}</th><th>{t("Column")}</th><th>{t("Grantor")}</th></tr></thead>
              <tbody>{readonly.map((g, i) => <tr key={i}><td>{g.grantee_type.toLowerCase()} {g.grantee}</td><td>{g.privilege}{g.grant_option ? " " + t("(with grant option)") : ""}</td><td>{g.object_type === "ddl" ? `${g.privilege} ${g.object.replace(/^SQL\$/, "")}` : `${g.object} (${g.object_type})`}</td><td>{g.column ?? ""}</td><td>{g.grantor ?? ""}</td></tr>)}</tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

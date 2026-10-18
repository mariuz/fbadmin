import { useEffect, useState } from "react";
import { api } from "../../api";
import { errMsg, keys, useApp } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { typeInfo } from "../../types";
import { oapi, type DropPreview } from "./api";
import { Area, DlgModal, Field, ObjectDialog, Text, useSpec } from "./common";

/** Object types that are browsed only (no drop from the tree). */
const READ_ONLY = new Set(["charsets", "shadows", "blob_filters", "system_tables"]);
export const canDrop = (otype: string) => !READ_ONLY.has(otype);

const label = (otype: string) => typeInfo(otype)?.singular.toLowerCase() ?? otype;

/** Drop with a dependency preview: "these 3 views and 1 procedure depend on it". */
export function DropDialog(props: { serverId: string; dbId: string; otype: string; name: string; column?: string }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid, otype, name, column } = props;
  const [p, setP] = useState<DropPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ack, setAck] = useState(false);
  useEffect(() => { oapi.dropPreview(sid, dbid, otype, name, column).then(setP).catch((e) => setError(errMsg(e))); }, []);
  const close = () => app.showDialog(null);
  const what = column ? `column ${name}.${column}` : `${label(otype)} ${name}`;
  const blockers = p ? p.dependents.length + p.foreign_keys.length + p.columns.length + (p.constraints?.length ?? 0) : 0;
  const counts: Record<string, number> = {};
  for (const d of p?.dependents ?? []) counts[d.type] = (counts[d.type] ?? 0) + 1;
  const summary = Object.entries(counts).map(([t, n]) => `${n} ${typeInfo(t)?.singular.toLowerCase() ?? t}${n > 1 ? "s" : ""}`.replace(/xs$/, "xes").replace(/ys$/, "ies"));

  const drop = async () => {
    if (!p) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.execute(sid, dbid, p.sql);
      if (r.error) throw new Error(r.error.message);
      app.toast(`Dropped ${what}`);
      app.invalidate(keys.folder(sid, dbid, otype));
      app.invalidate(keys.db(sid, dbid));
      if (column) {
        app.invalidate(keys.object(sid, dbid, otype, name));
        app.select({ kind: "object", serverId: sid, dbId: dbid, otype, name });
      } else app.select({ kind: "folder", serverId: sid, dbId: dbid, otype });
      close();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DlgModal title={`Drop ${what}`} onClose={close} size="wide" footer={<>
      {error && <span className="err">{error}</span>}
      <button className="btn" onClick={close}>Cancel</button>
      <button className="btn danger solid" disabled={!p || busy || (blockers > 0 && !ack)} onClick={drop}>Drop</button>
    </>}>
      {!p && !error && <div className="muted">Checking dependencies…</div>}
      {p && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {blockers === 0 ? (
            <div>Nothing depends on {what}. This cannot be undone.</div>
          ) : (
            <div className="warn-box">
              ⚠ {summary.length ? <>{summary.join(", ")} depend{p.dependents.length === 1 ? "s" : ""} on {what}. </> : null}
              {p.foreign_keys.length > 0 && <>{p.foreign_keys.length} foreign key(s) reference it. </>}
              {p.columns.length > 0 && <>{p.columns.length} column(s) use this domain. </>}
              {(p.constraints?.length ?? 0) > 0 && <>It is part of constraint(s) {p.constraints!.join(", ")}. </>}
              Firebird will refuse the drop until they are changed or dropped first.
            </div>
          )}
          {p.dependents.length > 0 && (
            <div className="grid-wrap" style={{ maxHeight: 220 }}>
              <table className="grid">
                <thead><tr><th>Dependent object</th><th>Type</th><th>Through</th></tr></thead>
                <tbody>{p.dependents.map((d, i) => (
                  <tr key={i}>
                    <td style={{ paddingLeft: 8 + ((d.depth ?? 1) - 1) * 16 }}>{(d.depth ?? 1) > 1 ? "↳ " : ""}{d.name}{d.detail ? ` (${d.detail})` : ""}</td>
                    <td>{typeInfo(d.type)?.singular ?? d.type}</td>
                    <td className="muted">{d.via}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {p.foreign_keys.length > 0 && <div className="small">Referencing foreign keys: {p.foreign_keys.map((f) => `${f.relation}.${f.name}`).join(", ")}</div>}
          {p.columns.length > 0 && <div className="small">Columns using the domain: {p.columns.map((c) => `${c.relation}.${c.name}`).join(", ")}</div>}
          {(p.indices?.length ?? 0) > 0 && <div className="small">Indices on the column (dropped with it): {p.indices!.join(", ")}</div>}
          {p.grants.length > 0 && <div className="small muted">{p.grants.length} privilege(s) granted on it will be removed.</div>}
          <div className="sql-preview short"><SqlEditor value={p.sql} readOnly /></div>
          {blockers > 0 && (
            <label className="check"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> Try to drop anyway</label>
          )}
        </div>
      )}
    </DlgModal>
  );
}

// --------------------------------------------------------------------- rename
export const RENAMABLE = new Set(["domains", "indices"]);
const WHY: Record<string, string> = {
  tables: "Firebird has no ALTER TABLE … RENAME. Create a new table, copy the data and drop the old one.",
  views: "Views cannot be renamed. Create the view under the new name and drop the old one.",
  sequences: "ALTER SEQUENCE cannot rename. Create a new sequence (SET GENERATOR to the current value) and update the code that uses it.",
  procedures: "Routines cannot be renamed; create a copy under the new name.",
  functions: "Routines cannot be renamed; create a copy under the new name.",
  packages: "Packages cannot be renamed; create a copy under the new name.",
  triggers: "Triggers cannot be renamed; create a copy and drop the old one.",
  exceptions: "Exceptions cannot be renamed; create a new one.",
  roles: "Roles cannot be renamed.",
  users: "Users cannot be renamed.",
  collations: "Collations cannot be renamed.",
};

export function RenameDialog(props: { serverId: string; dbId: string; otype: string; name: string; column?: string }) {
  const { serverId: sid, dbId: dbid, otype, name, column } = props;
  const kind = column ? "columns" : otype;
  const supported = kind === "columns" || RENAMABLE.has(kind);
  const { spec: idx } = useSpec<any>(sid, dbid, "indices", kind === "indices" ? name : undefined, () => null);
  const [newName, setNewName] = useState(column ?? name);
  const spec = !supported ? { otype: kind, name, new_name: "-" } : kind === "columns"
    ? { otype: "columns", table: name, name: column, new_name: newName }
    : kind === "indices" ? (idx ? { otype, name, new_name: newName, index: idx } : null)
      : { otype, name, new_name: newName };
  const unchanged = newName.trim() === (column ?? name) || !newName.trim();
  const tabs = [{
    id: "general", title: "General", render: () => (
      <div className="form">
        {!supported && <><span /><div className="warn-box">⚠ {WHY[kind] ?? "This object type cannot be renamed."}</div></>}
        <Field label="Current name"><span className="mono">{column ? `${name}.${column}` : name}</span></Field>
        <Field label="New name" hint={kind === "columns" ? "ALTER TABLE … ALTER COLUMN … TO. Columns used by views, procedures, triggers or computed columns cannot be renamed while those exist."
          : kind === "domains" ? "ALTER DOMAIN … TO" : kind === "indices" ? "Firebird has no ALTER INDEX … RENAME: the index is dropped and created again under the new name." : undefined}>
          <Text value={newName} upper disabled={!supported} onChange={setNewName} autoFocus />
        </Field>
      </div>
    ),
  }];
  return (
    <ObjectDialog size="" title={`Rename ${column ? "column" : label(otype)} ${column ?? name}`} serverId={sid} dbId={dbid} kind="rename"
      otype={otype} spec={spec} original={{ rename: true }} disabledReason={supported && unchanged ? "Enter a new name" : undefined}
      tabs={tabs} selectName={column ? undefined : newName} />
  );
}

// ------------------------------------------------------------------- comments
interface CommentSpec { otype: string; name: string; comment?: string | null; items: Array<{ name: string; kind: string; comment?: string | null }> }

/** COMMENT ON for any object, its columns and its parameters. */
export function CommentsDialog(props: { serverId: string; dbId: string; otype: string; name: string; focus?: string }) {
  const { serverId: sid, dbId: dbid, otype, name } = props;
  const [spec, setSpec] = useState<CommentSpec | null>(null);
  const [original, setOriginal] = useState<CommentSpec | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    oapi.comments(sid, dbid, otype, name).then((s) => { setOriginal(s); setSpec(JSON.parse(JSON.stringify(s))); }).catch((e) => setError(errMsg(e)));
  }, []);
  const tabs = !spec ? [] : [{
    id: "general", title: "Comments", render: () => (
      <>
        <div className="form">
          <Field label={`${typeInfo(otype)?.singular ?? otype} comment`}><Area rows={3} value={spec.comment} onChange={(v) => setSpec({ ...spec, comment: v })} /></Field>
        </div>
        {spec.items.length > 0 && (
          <table className="coltable" style={{ width: "100%", marginTop: 12 }}>
            <thead><tr className="muted"><td style={{ width: "30%" }}>{spec.items[0].kind === "column" ? "Column" : "Parameter"}</td><td>Comment</td></tr></thead>
            <tbody>{spec.items.map((it, i) => (
              <tr key={it.name}>
                <td className="mono">{it.name}</td>
                <td><Text value={it.comment} autoFocus={props.focus === it.name}
                  onChange={(v) => setSpec({ ...spec, items: spec.items.map((x, j) => (j === i ? { ...x, comment: v } : x)) })} /></td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </>
    ),
  }];
  return <ObjectDialog title={`Comments — ${name}`} serverId={sid} dbId={dbid} kind="comments" otype={otype} spec={spec} original={original}
    loadError={error} tabs={tabs} />;
}

import { useEffect, useState } from "react";
import { SqlEditor } from "../../components/SqlEditor";
import { IconPlus, IconTrash } from "../../components/Icons";
import { oapi } from "./api";
import {
  Area, Check, CollationSelect, DependentsNote, Field, ObjectDialog, Select, Text, TypePicker, parseType,
  useDependents, useLookups, useSpec,
} from "./common";

interface P { serverId: string; dbId: string; name?: string }

// ---------------------------------------------------------------------- view
interface ViewSpec { name: string; columns: string[]; select: string; check_option: boolean; comment?: string | null }

export function ViewDialog(props: P) {
  const { serverId: sid, dbId: dbid } = props;
  const { spec: v, setSpec, original, error } = useSpec<ViewSpec>(sid, dbid, "views", props.name,
    () => ({ name: "NEW_VIEW", columns: [], select: "SELECT\n  ID, NAME\nFROM SOME_TABLE", check_option: false }));
  const deps = useDependents(sid, dbid, "views", props.name);
  const set = (p: Partial<ViewSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const tabs = !v ? [] : [
    {
      id: "general", title: "General", render: () => (
        <div className="form">
          <Field label="Name" hint={props.name ? "Views cannot be renamed." : undefined}><Text value={v.name} upper disabled={!!props.name} onChange={(x) => set({ name: x })} autoFocus /></Field>
          <span />
          <Check checked={v.check_option} onChange={(x) => set({ check_option: x })}
            label={<>WITH CHECK OPTION <span className="muted">— inserts/updates through the view must satisfy its WHERE</span></>} />
          <Field label="Comment"><Area value={v.comment} onChange={(x) => set({ comment: x })} /></Field>
        </div>
      ),
    },
    {
      id: "definition", title: "Definition", render: () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {props.name && <DependentsNote deps={deps} />}
          <div className="form">
            <Field label="Column list" hint="Optional, comma separated. Leave empty to use the SELECT's column names.">
              <Text mono value={v.columns.join(", ")} onChange={(x) => set({ columns: x.trim() ? x.split(",").map((s) => s.trim().toUpperCase()) : [] })} />
            </Field>
          </div>
          <div className="editor-box"><SqlEditor value={v.select} onChange={(x) => set({ select: x })} /></div>
        </div>
      ),
    },
  ];
  return (
    <ObjectDialog title={props.name ? `View ${props.name}` : "Create view"} serverId={sid} dbId={dbid} kind="view" otype="views"
      spec={v} original={original} loadError={error} tabs={tabs} dependents={deps} selectName={props.name ? undefined : v?.name}
      initialTab={props.name ? "definition" : "general"} />
  );
}

// ------------------------------------------------------------------ sequence
interface SeqSpec { name: string; start?: number | string | null; increment?: number | string | null; current_value?: number | null;
  restart_with?: string | null; set_value?: string | null; comment?: string | null }

export function SequenceDialog(props: P) {
  const { serverId: sid, dbId: dbid } = props;
  const { spec: s, setSpec, original, error } = useSpec<SeqSpec>(sid, dbid, "sequences", props.name, () => ({ name: "NEW_SEQUENCE", start: 1, increment: 1 }));
  const set = (p: Partial<SeqSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const editing = !!props.name;
  const tabs = !s ? [] : [{
    id: "general", title: "General", render: () => (
      <div className="form">
        <Field label="Name" hint={editing ? "Firebird has no rename for sequences (ALTER SEQUENCE cannot rename)." : undefined}>
          <Text value={s.name} upper disabled={editing} onChange={(x) => set({ name: x })} autoFocus />
        </Field>
        {editing ? (
          <>
            <Field label="Current value"><span className="mono">{s.current_value ?? "—"}</span></Field>
            <Field label="Initial value"><span className="mono">{s.start ?? 0}</span></Field>
            <Field label="Restart with" hint="ALTER SEQUENCE … RESTART WITH (FB4+: the next value returned is this value)">
              <Text value={s.restart_with} onChange={(x) => set({ restart_with: x || null })} placeholder="(unchanged)" />
            </Field>
            <Field label="Set current value" hint="SET GENERATOR … TO (the next value is this + increment)">
              <Text value={s.set_value} onChange={(x) => set({ set_value: x || null })} placeholder="(unchanged)" />
            </Field>
          </>
        ) : (
          <Field label="Start with"><Text value={s.start == null ? "" : String(s.start)} onChange={(x) => set({ start: x })} /></Field>
        )}
        <Field label="Increment by"><Text value={s.increment == null ? "" : String(s.increment)} onChange={(x) => set({ increment: x })} /></Field>
        <Field label="Comment"><Area value={s.comment} onChange={(x) => set({ comment: x })} /></Field>
      </div>
    ),
  }];
  return (
    <ObjectDialog title={editing ? `Sequence ${props.name}` : "Create sequence"} size="" serverId={sid} dbId={dbid} kind="sequence" otype="sequences"
      spec={s} original={original} loadError={error} tabs={tabs} selectName={editing ? undefined : s?.name} />
  );
}

// -------------------------------------------------------------------- domain
interface DomSpec { name: string; data_type: string; default?: string | null; not_null?: boolean; check?: string | null;
  collation?: string | null; comment?: string | null; used_by?: Array<{ name: string; type: string }> }

export function DomainDialog(props: P) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: d, setSpec, original, error } = useSpec<DomSpec>(sid, dbid, "domains", props.name, () => ({ name: "NEW_DOMAIN", data_type: "VARCHAR(100)" }));
  const set = (p: Partial<DomSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const editing = !!props.name;
  const tabs = !d ? [] : [
    {
      id: "general", title: "General", render: () => (
        <div className="form">
          <Field label="Name" hint={editing ? "Renaming uses ALTER DOMAIN … TO" : undefined}><Text value={d.name} upper onChange={(x) => set({ name: x })} autoFocus /></Field>
          <Field label="Data type"><TypePicker value={d.data_type} lk={lk} onChange={(x) => set({ data_type: x })} /></Field>
          <Field label="Collation" hint={editing ? "Cannot be changed after creation." : undefined}>
            <CollationSelect value={d.collation} lk={lk} charset={parseType(d.data_type).charset || null} onChange={(x) => set({ collation: x })} />
          </Field>
          <Field label="Comment"><Area value={d.comment} onChange={(x) => set({ comment: x })} /></Field>
        </div>
      ),
    },
    {
      id: "constraints", title: "Constraints", render: () => (
        <div className="form">
          <Field label="Default"><Text mono value={d.default} onChange={(x) => set({ default: x || null })} placeholder="e.g. 0" /></Field>
          <span />
          <Check checked={!!d.not_null} onChange={(x) => set({ not_null: x })} label="NOT NULL" />
          <Field label="Check" hint="Use VALUE for the value being checked."><Area mono value={d.check} onChange={(x) => set({ check: x || null })} placeholder="VALUE > 0" /></Field>
          {editing && d.used_by && d.used_by.length > 0 && <Field label="Used by"><span className="small">{d.used_by.map((u) => u.name).join(", ")}</span></Field>}
        </div>
      ),
    },
  ];
  return (
    <ObjectDialog title={editing ? `Domain ${props.name}` : "Create domain"} serverId={sid} dbId={dbid} kind="domain" otype="domains"
      spec={d} original={original} loadError={error} tabs={tabs} selectName={d?.name !== props.name ? d?.name : undefined} />
  );
}

// ----------------------------------------------------------------- exception
interface ExcSpec { name: string; message: string; number?: number; comment?: string | null }

export function ExceptionDialog(props: P) {
  const { serverId: sid, dbId: dbid } = props;
  const { spec: x, setSpec, original, error } = useSpec<ExcSpec>(sid, dbid, "exceptions", props.name, () => ({ name: "NEW_EXCEPTION", message: "Something went wrong: @1" }));
  const set = (p: Partial<ExcSpec>) => setSpec((o) => (o ? { ...o, ...p } : o));
  const tabs = !x ? [] : [{
    id: "general", title: "General", render: () => (
      <div className="form">
        <Field label="Name" hint={props.name ? "Exceptions cannot be renamed." : undefined}><Text value={x.name} upper disabled={!!props.name} onChange={(v) => set({ name: v })} autoFocus /></Field>
        {x.number !== undefined && <Field label="Number"><span className="mono">{x.number}</span></Field>}
        <Field label="Message" hint="@1, @2 … are replaced by the arguments of EXCEPTION name USING (…)"><Area value={x.message} onChange={(v) => set({ message: v })} /></Field>
        <Field label="Comment"><Area value={x.comment} onChange={(v) => set({ comment: v })} /></Field>
      </div>
    ),
  }];
  return (
    <ObjectDialog title={props.name ? `Exception ${props.name}` : "Create exception"} size="" serverId={sid} dbId={dbid} kind="exception" otype="exceptions"
      spec={x} original={original} loadError={error} tabs={tabs} selectName={props.name ? undefined : x?.name} />
  );
}

// ---------------------------------------------------------------------- role
interface RoleSpec { name: string; members: Array<{ name: string; type: string; admin: boolean }>; system_privileges: string[] | null; comment?: string | null }

export function RoleDialog(props: P) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: r, setSpec, original, error } = useSpec<RoleSpec>(sid, dbid, "roles", props.name, () => ({ name: "NEW_ROLE", members: [], system_privileges: [] }));
  const [grantees, setGrantees] = useState<Record<string, string[]>>({});
  useEffect(() => { oapi.grantees(sid, dbid).then(setGrantees).catch(() => undefined); }, []);
  const set = (p: Partial<RoleSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const engine = lk?.engine_major ?? 5;
  const tabs = !r ? [] : [
    {
      id: "general", title: "General", render: () => (
        <div className="form">
          <Field label="Name" hint={props.name ? "Roles cannot be renamed." : undefined}><Text value={r.name} upper disabled={!!props.name} onChange={(x) => set({ name: x })} autoFocus /></Field>
          <Field label="Comment"><Area value={r.comment} onChange={(x) => set({ comment: x })} /></Field>
        </div>
      ),
    },
    {
      id: "members", title: `Members (${r.members.length})`, render: () => (
        <>
          <datalist id="role-grantees">{[...(grantees.users ?? []), ...(grantees.roles ?? [])].map((g) => <option key={g} value={g} />)}</datalist>
          <table className="coltable" style={{ width: "100%" }}>
            <thead><tr className="muted"><td>Grantee</td><td style={{ width: 120 }}>Type</td><td>WITH ADMIN OPTION</td><td /></tr></thead>
            <tbody>
              {r.members.map((m, i) => (
                <tr key={i}>
                  <td><Text value={m.name} upper list="role-grantees" onChange={(x) => set({ members: r.members.map((y, j) => (j === i ? { ...y, name: x } : y)) })} /></td>
                  <td><Select value={m.type} options={engine >= 4 ? ["USER", "ROLE"] : ["USER"]} onChange={(x) => set({ members: r.members.map((y, j) => (j === i ? { ...y, type: x } : y)) })} /></td>
                  <td style={{ textAlign: "center" }}><input type="checkbox" checked={m.admin} onChange={(e) => set({ members: r.members.map((y, j) => (j === i ? { ...y, admin: e.target.checked } : y)) })} /></td>
                  <td><button className="btn icon small" title="Revoke" onClick={() => set({ members: r.members.filter((_, j) => j !== i) })}><IconTrash size={13} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="btn small" style={{ marginTop: 8 }} onClick={() => set({ members: [...r.members, { name: "", type: "USER", admin: false }] })}><IconPlus size={13} /> Add member</button>
        </>
      ),
    },
    {
      id: "sysprivs", title: "System privileges", render: () => engine < 4 ? <div className="muted">System privileges need Firebird 4 or newer.</div> : (
        <div className="sysprivs">
          {(lk?.system_privileges ?? []).map((p) => (
            <Check key={p} label={p} checked={(r.system_privileges ?? []).includes(p)}
              onChange={(on) => set({ system_privileges: on ? [...(r.system_privileges ?? []), p] : (r.system_privileges ?? []).filter((x) => x !== p) })} />
          ))}
        </div>
      ),
    },
  ];
  return (
    <ObjectDialog title={props.name ? `Role ${props.name}` : "Create role"} serverId={sid} dbId={dbid} kind="role" otype="roles"
      spec={r} original={original} loadError={error} tabs={tabs} selectName={props.name ? undefined : r?.name} />
  );
}

// ----------------------------------------------------------------- collation
interface CollSpec { name: string; charset: string; base?: string | null; external?: string | null; pad_space?: boolean;
  case_insensitive?: boolean; accent_insensitive?: boolean; attributes?: string | null; comment?: string | null; system?: boolean }

export function CollationDialog(props: P) {
  const { serverId: sid, dbId: dbid } = props;
  const lk = useLookups(sid, dbid);
  const { spec: c, setSpec, original, error } = useSpec<CollSpec>(sid, dbid, "collations", props.name,
    () => ({ name: "UNICODE_NUM_CI", charset: "UTF8", base: "UNICODE", pad_space: false, case_insensitive: true, accent_insensitive: false, attributes: "NUMERIC-SORT=1" }));
  const set = (p: Partial<CollSpec>) => setSpec((x) => (x ? { ...x, ...p } : x));
  const editing = !!props.name;
  const bases = (lk?.collations ?? []).filter((x) => x.charset === c?.charset).map((x) => x.name);
  const tabs = !c ? [] : [{
    id: "general", title: "General", render: () => (
      <div className="form">
        {editing && <><span /><div className="warn-box">Collations cannot be altered; only the comment can be changed. Drop and re-create the collation to change it.</div></>}
        <Field label="Name"><Text value={c.name} upper disabled={editing} onChange={(x) => set({ name: x })} autoFocus /></Field>
        <Field label="Character set"><Select value={c.charset} disabled={editing} onChange={(x) => set({ charset: x, base: null })} options={lk?.charsets ?? [c.charset]} /></Field>
        <Field label="From collation"><Select value={c.base} disabled={editing || !!c.external} onChange={(x) => set({ base: x || null })} options={bases} empty="(none)" /></Field>
        <Field label="From external" hint="FROM EXTERNAL ('name') — a collation from fbintl.conf"><Text value={c.external} disabled={editing} onChange={(x) => set({ external: x || null })} /></Field>
        <span />
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
          <Check disabled={editing} checked={!!c.pad_space} onChange={(x) => set({ pad_space: x })} label="PAD SPACE" />
          <Check disabled={editing} checked={!!c.case_insensitive} onChange={(x) => set({ case_insensitive: x })} label="Case insensitive" />
          <Check disabled={editing} checked={!!c.accent_insensitive} onChange={(x) => set({ accent_insensitive: x })} label="Accent insensitive" />
        </div>
        <Field label="Attributes" hint="ICU specific attributes, e.g. NUMERIC-SORT=1;LOCALE=de_DE"><Text mono value={c.attributes} disabled={editing} onChange={(x) => set({ attributes: x || null })} /></Field>
        <Field label="Comment"><Area value={c.comment} onChange={(x) => set({ comment: x })} /></Field>
      </div>
    ),
  }];
  return (
    <ObjectDialog title={editing ? `Collation ${props.name}` : "Create collation"} serverId={sid} dbId={dbid} kind="collation" otype="collations"
      spec={c} original={original} loadError={error} tabs={tabs} selectName={editing ? undefined : c?.name} />
  );
}

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api";
import { errMsg, keys, useApp } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { IconTerminal, IconX } from "../../components/Icons";
import { typeInfo } from "../../types";
import { getLookups, oapi, type DdlScript, type Dependent, type Lookups } from "./api";
import { t } from "../../i18n";

export const quote = (n: string) => (/^[A-Z][A-Z0-9_$]*$/.test(n) ? n : `"${n.replace(/"/g, '""')}"`);

/** Modal with an optional size class (the core Modal only knows "wide"). */
export function DlgModal(props: {
  title: ReactNode; children: ReactNode; footer?: ReactNode; onClose: () => void; size?: "" | "wide" | "xl";
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && props.onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props.onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && props.onClose()}>
      <div className={"modal objdlg " + (props.size ?? "")} role="dialog" aria-modal="true">
        <div className="modal-head">{props.title}</div>
        <div className="modal-body">{props.children}</div>
        {props.footer && <div className="modal-foot">{props.footer}</div>}
      </div>
    </div>
  );
}

export function useLookups(sid: string, dbid: string): Lookups | null {
  const [lk, setLk] = useState<Lookups | null>(null);
  useEffect(() => { getLookups(sid, dbid).then(setLk).catch(() => setLk(null)); }, [sid, dbid]);
  return lk;
}

/** Load the editable spec of an existing object (or use ``empty`` for a new one). */
export function useSpec<T>(sid: string, dbid: string, otype: string, name: string | undefined, empty: () => T) {
  const [spec, setSpec] = useState<T | null>(name ? null : empty());
  const [original, setOriginal] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!name) return;
    oapi.spec<T>(sid, dbid, otype, name)
      .then((r) => { setOriginal(r.spec); setSpec(JSON.parse(JSON.stringify(r.spec))); })
      .catch((e) => setError(errMsg(e)));
  }, [sid, dbid, otype, name]);
  return { spec, setSpec, original, error };
}

/** Debounced DDL generation for the SQL tab. */
export function useDdl(sid: string, dbid: string, kind: string, spec: unknown, original: unknown) {
  const [res, setRes] = useState<(DdlScript & { error?: string }) | null>(null);
  const key = JSON.stringify([kind, spec, original]);
  useEffect(() => {
    if (spec === null || spec === undefined) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const r = await oapi.ddl(sid, dbid, kind, spec, original);
        if (!cancelled) setRes(r);
      } catch (e) {
        if (!cancelled) setRes({ sql: "", statements: [], warnings: [], error: errMsg(e) });
      }
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [key]);
  return res;
}

export interface DlgTab { id: string; title: string; render: () => ReactNode }

/**
 * pgAdmin-style object dialog: form tabs + a SQL tab with the exact DDL. Save regenerates the
 * DDL, runs it through /execute (autocommit) and refreshes the tree.
 */
export function ObjectDialog(props: {
  title: ReactNode;
  serverId: string;
  dbId: string;
  kind: string;
  spec: any;
  original?: any;
  tabs: DlgTab[];
  loadError?: string | null;
  /** object type folder to refresh after saving */
  otype: string;
  /** name to select after saving (new objects) */
  selectName?: string;
  /** objects that depend on this one: a warning is shown before an ALTER runs */
  dependents?: Dependent[];
  size?: "" | "wide" | "xl";
  extraButtons?: ReactNode;
  onSaved?: () => void;
  initialTab?: string;
  /** when set, Save is disabled with this tooltip */
  disabledReason?: string;
}) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [tabSel, setTab] = useState<string | null>(null);
  const tab = tabSel ?? props.initialTab ?? props.tabs[0]?.id ?? "sql";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDeps, setConfirmDeps] = useState(false);
  const ddl = useDdl(sid, dbid, props.kind, props.spec, props.original ?? null);
  const close = () => app.showDialog(null);
  const editing = !!props.original;
  const deps = props.dependents ?? [];

  const save = async () => {
    setError(null);
    if (editing && deps.length && !confirmDeps) {
      setConfirmDeps(true);
      return;
    }
    setBusy(true);
    try {
      const script = await oapi.ddl(sid, dbid, props.kind, props.spec, props.original ?? null);
      if (!script.sql.trim()) { close(); return; }
      const r = await api.execute(sid, dbid, script.sql);
      if (r.error) {
        setTab("sql");
        throw new Error(`${t("Statement {n} failed:", { n: r.error.index + 1 })}\n${r.error.message}`);
      }
      app.invalidate(keys.folder(sid, dbid, props.otype));
      app.invalidate(keys.db(sid, dbid));
      const ti = typeInfo(props.otype);
      app.toast(editing ? t("Changes applied") : t("{object} created", { object: ti ? t(ti.singular) : t("Object") }));
      if (props.selectName) {
        app.select({ kind: "object", serverId: sid, dbId: dbid, otype: props.otype, name: props.selectName });
      } else if (app.selection) app.select({ ...app.selection });
      props.onSaved?.();
      close();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const tabs = [...props.tabs, { id: "sql", title: t("SQL"), render: () => <SqlTab ddl={ddl} /> }];
  const nothing = !!ddl && !ddl.error && !ddl.sql.trim();
  return (
    <DlgModal
      size={props.size ?? "wide"}
      title={props.title}
      onClose={close}
      footer={
        <>
          {error && <span className="err">{error}</span>}
          {!error && confirmDeps && (
            <span className="warn" style={{ marginRight: "auto" }}>
              {t("{count} object(s) depend on this one ({names}). Altering it may fail or invalidate them. Click again to continue.", {
                count: deps.length,
                names: deps.slice(0, 4).map((d) => d.name).join(", ") + (deps.length > 4 ? ", …" : ""),
              })}
            </span>
          )}
          {props.extraButtons}
          <button className="btn" disabled={!ddl?.sql} onClick={() => { app.openQueryTool(sid, dbid, ddl?.sql); close(); }}>
            <IconTerminal /> {t("Query Tool")}
          </button>
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy || props.spec === null || !!ddl?.error || nothing || !!props.disabledReason} onClick={save}
            title={props.disabledReason ?? (nothing ? t("Nothing changed") : undefined)}>
            {confirmDeps ? t("Save anyway") : editing ? t("Save") : t("Create")}
          </button>
        </>
      }
    >
      {props.loadError ? <div className="err">{props.loadError}</div> : props.spec === null ? <div className="muted">{t("Loading…")}</div> : (
        <>
          <div className="subtabs">
            {tabs.map((tb) => (
              <button key={tb.id} className={tab === tb.id ? "active" : ""} onClick={() => setTab(tb.id)}>
                {tb.title}{tb.id === "sql" && ddl?.warnings.length ? " ⚠" : ""}
              </button>
            ))}
          </div>
          <div className="objdlg-body">{tabs.find((tb) => tb.id === tab)?.render()}</div>
        </>
      )}
    </DlgModal>
  );
}

export function SqlTab(props: { ddl: (DdlScript & { error?: string }) | null }) {
  const { ddl } = props;
  if (!ddl) return <div className="muted">{t("Generating…")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {ddl.error && <div className="err" style={{ whiteSpace: "pre-wrap" }}>{ddl.error}</div>}
      {ddl.warnings.map((w, i) => <div key={i} className="warn-box">⚠ {w}</div>)}
      {!ddl.error && !ddl.sql.trim() && <div className="muted">{t("No changes — nothing to run.")}</div>}
      <div className="sql-preview"><SqlEditor value={ddl.sql} readOnly /></div>
    </div>
  );
}

// ------------------------------------------------------------------ form bits
export function Field(props: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <>
      <label className="l">{props.label}</label>
      <div>
        {props.children}
        {props.hint && <div className="muted small">{props.hint}</div>}
      </div>
    </>
  );
}

export function Text(props: { value: string | null | undefined; onChange: (v: string) => void; upper?: boolean;
  placeholder?: string; disabled?: boolean; autoFocus?: boolean; mono?: boolean; list?: string; title?: string }) {
  return (
    <input className={"text" + (props.mono ? " mono" : "")} value={props.value ?? ""} placeholder={props.placeholder}
      disabled={props.disabled} autoFocus={props.autoFocus} list={props.list} title={props.title}
      style={{ width: "100%" }}
      onChange={(e) => props.onChange(props.upper ? e.target.value.toUpperCase() : e.target.value)} />
  );
}

export function Area(props: { value: string | null | undefined; onChange: (v: string) => void; rows?: number;
  placeholder?: string; mono?: boolean }) {
  return (
    <textarea className={"text" + (props.mono ? " mono" : "")} rows={props.rows ?? 3} value={props.value ?? ""}
      placeholder={props.placeholder} style={{ width: "100%", resize: "vertical" }}
      onChange={(e) => props.onChange(e.target.value)} />
  );
}

export function Check(props: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; disabled?: boolean; title?: string }) {
  return (
    <label className="check" title={props.title} style={props.disabled ? { opacity: 0.55 } : undefined}>
      <input type="checkbox" checked={!!props.checked} disabled={props.disabled} onChange={(e) => props.onChange(e.target.checked)} />
      {props.label}
    </label>
  );
}

export function Select(props: { value: string | null | undefined; onChange: (v: string) => void;
  options: Array<string | [string, string]>; disabled?: boolean; empty?: string }) {
  return (
    <select className="text" value={props.value ?? ""} disabled={props.disabled} style={{ width: "100%" }}
      onChange={(e) => props.onChange(e.target.value)}>
      {props.empty !== undefined && <option value="">{props.empty}</option>}
      {props.options.map((o) => {
        const [v, l] = Array.isArray(o) ? o : [o, o];
        return <option key={v} value={v}>{l}</option>;
      })}
    </select>
  );
}

export function SecuritySelect(props: { value: string | null | undefined; onChange: (v: string | null) => void; engine: number }) {
  return (
    <Select value={props.value ?? ""} disabled={props.engine < 4}
      onChange={(v) => props.onChange(v || null)}
      empty={props.engine < 4 ? t("(Firebird 4+ only)") : t("(default)")} options={["DEFINER", "INVOKER"]} />
  );
}

// ---------------------------------------------------------------- data types
export const BASE_TYPES = ["SMALLINT", "INTEGER", "BIGINT", "INT128", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE PRECISION",
  "DECFLOAT", "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIME WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE", "CHAR",
  "VARCHAR", "BINARY", "VARBINARY", "BLOB SUB_TYPE TEXT", "BLOB SUB_TYPE BINARY"];
const SIZED = new Set(["NUMERIC", "DECIMAL", "CHAR", "VARCHAR", "BINARY", "VARBINARY", "DECFLOAT", "FLOAT"]);
const CHARSET_TYPES = new Set(["CHAR", "VARCHAR", "BLOB SUB_TYPE TEXT"]);
const FB4_TYPES = new Set(["INT128", "DECFLOAT", "TIME WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE", "BINARY", "VARBINARY"]);

interface ParsedType { base: string; size: string; charset: string; custom: boolean }

export function parseType(type: string | null | undefined): ParsedType {
  const s = (type ?? "").trim().toUpperCase();
  if (!s) return { base: "INTEGER", size: "", charset: "", custom: false };
  const m = /^(.+?)(?:\s*\(([\d\s,]+)\))?(?:\s+CHARACTER SET\s+(\w+))?$/.exec(s);
  if (m && BASE_TYPES.includes(m[1].trim())) return { base: m[1].trim(), size: (m[2] ?? "").replace(/\s/g, ""), charset: m[3] ?? "", custom: false };
  return { base: s, size: "", charset: "", custom: true };
}

export function formatType(p: ParsedType): string {
  if (p.custom) return p.base;
  let s = p.base;
  if (SIZED.has(p.base) && p.size) s += `(${p.size})`;
  if (CHARSET_TYPES.has(p.base) && p.charset) s += ` CHARACTER SET ${p.charset}`;
  return s;
}

/** Data type editor: base type + size/scale + character set, or free text. */
export function TypePicker(props: { value: string | null | undefined; onChange: (v: string) => void; lk: Lookups | null; compact?: boolean }) {
  const p = parseType(props.value);
  const set = (q: Partial<ParsedType>) => props.onChange(formatType({ ...p, ...q }));
  const engine = props.lk?.engine_major ?? 5;
  return (
    <div className="type-picker">
      <select className="text" value={p.custom ? "__custom" : p.base} onChange={(e) => {
        const v = e.target.value;
        if (v === "__custom") set({ custom: true, base: formatType(p) });
        else set({ base: v, custom: false, size: SIZED.has(v) ? (v === "VARCHAR" || v === "CHAR" ? p.size || "50" : p.size) : "" });
      }}>
        {BASE_TYPES.map((ty) => <option key={ty} value={ty} disabled={engine < 4 && FB4_TYPES.has(ty)}>{ty}</option>)}
        <option value="__custom">{t("custom…")}</option>
      </select>
      {p.custom ? (
        <input className="text mono" value={p.base} placeholder={t("e.g. VARCHAR(20) CHARACTER SET WIN1252")}
          onChange={(e) => props.onChange(e.target.value.toUpperCase())} />
      ) : (
        <>
          {SIZED.has(p.base) && (
            <input className="text" style={{ width: 64 }} value={p.size} title={t("length or precision,scale")}
              placeholder={p.base === "NUMERIC" || p.base === "DECIMAL" ? "18,2" : t("size")} onChange={(e) => set({ size: e.target.value })} />
          )}
          {CHARSET_TYPES.has(p.base) && !props.compact && (
            <select className="text" value={p.charset} onChange={(e) => set({ charset: e.target.value })} title={t("character set")}>
              <option value="">{props.lk ? t("(db default: {charset})", { charset: props.lk.default_charset }) : t("(db default)")}</option>
              {(props.lk?.charsets ?? []).map((c) => <option key={c}>{c}</option>)}
            </select>
          )}
        </>
      )}
    </div>
  );
}

export function CollationSelect(props: { value: string | null | undefined; onChange: (v: string | null) => void;
  lk: Lookups | null; charset?: string | null }) {
  const cs = props.charset || props.lk?.default_charset;
  const opts = (props.lk?.collations ?? []).filter((c) => !cs || c.charset === cs).map((c) => c.name);
  return <Select value={props.value ?? ""} onChange={(v) => props.onChange(v || null)} empty={t("(default)")} options={opts} />;
}

/** Ordered multi-column picker (key columns, index segments, ...). */
export function ColumnsPicker(props: { value: string[]; onChange: (v: string[]) => void; columns: string[]; placeholder?: string }) {
  const rest = props.columns.filter((c) => !props.value.includes(c));
  return (
    <div className="cols-picker">
      {props.value.map((c, i) => (
        <span key={c} className="chip">
          {c}
          {i > 0 && <button title={t("Move left")} onClick={() => { const v = [...props.value]; [v[i - 1], v[i]] = [v[i], v[i - 1]]; props.onChange(v); }}>‹</button>}
          <button title={t("Remove")} onClick={() => props.onChange(props.value.filter((x) => x !== c))}><IconX size={10} /></button>
        </span>
      ))}
      <select className="text" value="" onChange={(e) => e.target.value && props.onChange([...props.value, e.target.value])}>
        <option value="">{props.placeholder ?? t("+ column")}</option>
        {rest.map((c) => <option key={c}>{c}</option>)}
      </select>
    </div>
  );
}

/** Dependents of an existing object, for the "depends on it" warning before ALTER. */
export function useDependents(sid: string, dbid: string, otype: string, name?: string): Dependent[] {
  const [deps, setDeps] = useState<Dependent[]>([]);
  useEffect(() => {
    if (!name) return;
    api.object(sid, dbid, otype, name)
      .then((o) => setDeps((o.dependencies?.dependents ?? []).map((d) => ({ name: d.name, type: d.type, field: d.field }))))
      .catch(() => setDeps([]));
  }, [sid, dbid, otype, name]);
  return deps;
}

export function DependentsNote(props: { deps: Dependent[] }) {
  const names = useMemo(() => Array.from(new Set(props.deps.map((d) => {
    const ti = typeInfo(d.type);
    return `${d.name} (${ti ? t(ti.singular).toLowerCase() : d.type})`;
  }))), [props.deps]);
  if (!names.length) return null;
  return <div className="warn-box">⚠ {t("Used by {count} object(s): {names}. Changing the signature or result columns may fail or invalidate them.", { count: names.length, names: names.slice(0, 8).join(", ") + (names.length > 8 ? ", …" : "") })}</div>;
}

export const move = <T,>(arr: T[], i: number, d: number): T[] => {
  const j = i + d;
  if (j < 0 || j >= arr.length) return arr;
  const a = [...arr];
  [a[i], a[j]] = [a[j], a[i]];
  return a;
};

import { useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import { typeInfo } from "../../types";
import { SqlEditor } from "../../components/SqlEditor";
import { IconCopy, IconDownload, IconRefresh, IconTerminal } from "../../components/Icons";
import { saveBlob } from "../schema/api";
import { SideBySide } from "../schema/common";
import { fmtTime, mapi, type DriftItem, type DriftOptions, type DriftReport, type DriftStatus } from "./api";
import { Section } from "./common";

/** Object types a drift check can ignore (DriftOptions.ignore_types on the backend). */
const IGNORABLE: Array<[string, string]> = [
  ["comments", N_("Comments")], ["grants", N_("Grants")], ["indices", N_("Indices")], ["triggers", N_("Triggers")],
  ["views", N_("Views")], ["procedures", N_("Procedures")], ["functions", N_("Functions")], ["packages", N_("Packages")],
  ["sequences", N_("Sequences")], ["domains", N_("Domains")], ["exceptions", N_("Exceptions")], ["roles", N_("Roles")],
  ["collations", N_("Collations")], ["tables", N_("Tables")], ["database", N_("Database settings")],
];
export const DRIFT_STATUS: Record<DriftStatus, string> = {
  missing: N_("Missing"), extra: N_("Extra"), changed: N_("Changed"),
};
const STATUS_HINT: Record<DriftStatus, string> = {
  missing: N_("In the baseline, not in the database"), extra: N_("In the database, not in the baseline"),
  changed: N_("Different in the database"),
};

export interface DriftForm {
  baseline: "history" | "database"; history_db: string; ref: string; fetch: boolean;
  server_id: string; database_id: string; ignore_types: string[]; ignore_names: string;
}

export function defaultDriftForm(): DriftForm {
  return { baseline: "history", history_db: "", ref: "", fetch: false, server_id: "", database_id: "", ignore_types: [], ignore_names: "" };
}

export function toDriftForm(o: DriftOptions | null | undefined): DriftForm {
  if (!o) return defaultDriftForm();
  return {
    baseline: o.baseline, history_db: o.history_server_id && o.history_database_id ? `${o.history_server_id}/${o.history_database_id}` : "",
    ref: o.ref ?? "", fetch: !!o.fetch, server_id: o.server_id ?? "", database_id: o.database_id ?? "",
    ignore_types: o.ignore_types ?? [], ignore_names: (o.ignore_names ?? []).join(", "),
  };
}

export function driftPayload(f: DriftForm): Partial<DriftOptions> {
  const [hs, hd] = f.history_db ? f.history_db.split("/") : [null, null];
  return {
    baseline: f.baseline, history_server_id: f.baseline === "history" ? hs : null, history_database_id: f.baseline === "history" ? hd : null,
    ref: f.baseline === "history" ? f.ref.trim() || null : null, fetch: f.baseline === "history" && f.fetch,
    server_id: f.baseline === "database" ? f.server_id || null : null, database_id: f.baseline === "database" ? f.database_id || null : null,
    ignore_types: f.ignore_types, ignore_names: f.ignore_names.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean),
  };
}

/** Options of a schema_drift schedule: the baseline and what to ignore. */
export function DriftOptionsForm(props: { value: DriftForm; onChange: (f: DriftForm) => void; serverId: string; databaseId: string }) {
  const app = useApp();
  const f = props.value;
  const set = (p: Partial<DriftForm>) => props.onChange({ ...f, ...p });
  const others = app.servers.flatMap((s) => s.databases.filter((d) => !(s.id === props.serverId && d.id === props.databaseId))
    .map((d) => ({ value: `${s.id}/${d.id}`, label: `${s.name} / ${d.name}` })));
  const srv = app.servers.find((s) => s.id === f.server_id);
  return (
    <div className="form mform">
      <span className="l">{t("Compare with")}</span>
      <div className="mt-row" role="radiogroup" aria-label={t("Compare with")}>
        <label className="check"><input type="radio" name="drift-baseline" checked={f.baseline === "history"} onChange={() => set({ baseline: "history" })} /> {t("Schema history (git)")}</label>
        <label className="check"><input type="radio" name="drift-baseline" checked={f.baseline === "database"} onChange={() => set({ baseline: "database", server_id: f.server_id || props.serverId })} /> {t("Another database")}</label>
      </div>
      {f.baseline === "history" ? <>
        <label className="l" htmlFor="drift-hist">{t("Repository")}</label>
        <select id="drift-hist" className="text" value={f.history_db} onChange={(e) => set({ history_db: e.target.value })}>
          <option value="">{t("this database's schema history")}</option>
          {others.map((o) => <option key={o.value} value={o.value}>{t("schema history of {name}", { name: o.label })}</option>)}
        </select>
        <label className="l" htmlFor="drift-ref">{t("Branch, tag or commit")}</label>
        <input id="drift-ref" className="text mono" value={f.ref} placeholder={t("default: the configured branch")} onChange={(e) => set({ ref: e.target.value })} />
        <span />
        <label className="check" title={t("Fetch the repository's remote before comparing, so origin/<branch> is current")}>
          <input type="checkbox" checked={f.fetch} onChange={(e) => set({ fetch: e.target.checked })} /> {t("Fetch the remote first (for origin/<branch>)")}
        </label>
      </> : <>
        <label className="l">{t("Baseline database")}</label>
        <div className="mt-row">
          <select className="text" aria-label={t("Server")} value={f.server_id} onChange={(e) => set({ server_id: e.target.value, database_id: "" })}>
            {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <select className="text" style={{ flex: 1 }} aria-label={t("Baseline database")} value={f.database_id} onChange={(e) => set({ database_id: e.target.value })}>
            <option value="">{t("choose…")}</option>
            {(srv?.databases ?? []).filter((d) => !(srv?.id === props.serverId && d.id === props.databaseId))
              .map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </div>
      </>}
      <span className="l">{t("Ignore")}</span>
      <div className="drift-ignore">
        {IGNORABLE.map(([k, label]) => (
          <label key={k} className="check"><input type="checkbox" checked={f.ignore_types.includes(k)}
            onChange={(e) => set({ ignore_types: e.target.checked ? [...f.ignore_types, k] : f.ignore_types.filter((x) => x !== k) })} /> {t(label)}</label>
        ))}
      </div>
      <label className="l" htmlFor="drift-names">{t("Ignore names")}</label>
      <input id="drift-names" className="text mono" value={f.ignore_names} placeholder="TMP_*, *_BAK" title={t("Patterns with * and ?, separated by commas; case-insensitive")}
        onChange={(e) => set({ ignore_names: e.target.value })} />
    </div>
  );
}

export function driftResult(summary: Record<DriftStatus, number> | undefined, ignored?: number): string {
  if (!summary) return "";
  const parts = (Object.keys(DRIFT_STATUS) as DriftStatus[]).filter((k) => summary[k]).map((k) => `${summary[k]} ${t(DRIFT_STATUS[k]).toLowerCase()}`);
  const head = parts.length ? t("drift: {parts}", { parts: parts.join(", ") }) : t("no drift");
  return ignored ? `${head} · ${t("{count} ignored", { count: ignored })}` : head;
}

/** The latest report of a drift schedule: differences, both DDL versions, and the script. */
export function DriftReportView(props: { scheduleId: string; refreshKey: number }) {
  const app = useApp();
  const [rep, setRep] = useState<DriftReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<DriftItem | null>(null);
  const [view, setView] = useState<"objects" | "script">("objects");
  const load = () => mapi.driftReport(props.scheduleId)
    .then((r) => { setRep(r); setError(null); setSel((cur) => r.items.find((i) => cur && i.type === cur.type && i.name === cur.name && i.schema === cur.schema) ?? r.items[0] ?? null); })
    .catch((e) => { setRep(null); setError(errMsg(e)); });
  useEffect(() => { load(); }, [props.scheduleId, props.refreshKey]);
  const groups = useMemo(() => {
    const g: Record<string, DriftItem[]> = {};
    for (const i of rep?.items ?? []) (g[i.type] ??= []).push(i);
    return g;
  }, [rep]);
  return (
    <Section wide title={t("Drift report")} right={<button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>}>
      {!rep ? <div className="muted">{error ?? t("Loading…")}</div> : <>
        <div className="drift-head">
          <span className={"pill " + (rep.drift ? "drift" : "finished")}>{rep.drift ? t("drift") : t("no drift")}</span>
          <span>{t("{database} compared with {baseline}", { database: rep.database, baseline: rep.baseline.label })}</span>
          <span className="muted">{t("checked {time}", { time: fmtTime(rep.checked_at) })}</span>
          {rep.ignored > 0 && <span className="muted">{t("{count} ignored", { count: rep.ignored })}</span>}
        </div>
        {rep.drift && <>
          <div className="toolbar drift-bar">
            <div className="seg" role="tablist">
              <button role="tab" aria-selected={view === "objects"} className={view === "objects" ? "on" : ""} onClick={() => setView("objects")}>{t("Differences ({n})", { n: rep.items.length })}</button>
              <button role="tab" aria-selected={view === "script"} className={view === "script" ? "on" : ""} onClick={() => setView("script")}>{t("Script")}</button>
            </div>
            {(Object.keys(DRIFT_STATUS) as DriftStatus[]).map((k) => (
              <span key={k} className={"drift-status " + k} title={t(STATUS_HINT[k])}>{t(DRIFT_STATUS[k])} <b>{rep.summary[k] ?? 0}</b></span>
            ))}
            <span className="grow" />
            <button className="btn small" onClick={() => saveBlob(new Blob([rep.script], { type: "application/sql" }), `drift-${rep.database}.sql`)}><IconDownload size={12} /> {t("Download")}</button>
            <button className="btn small" onClick={() => { navigator.clipboard?.writeText(rep.script); app.toast(t("Script copied"), "info"); }}><IconCopy size={12} /> {t("Copy")}</button>
            <button className="btn small" title={t("Review and run the script that makes the database match the baseline")}
              onClick={() => app.openQueryTool(rep.server_id, rep.database_id, rep.script)}><IconTerminal size={12} /> {t("Open in Query Tool")}</button>
          </div>
          {rep.truncated && <div className="muted">{t("Only the first {n} differences are listed; the summary and the script cover all of them.", { n: rep.items.length })}</div>}
          {rep.warnings.length > 0 && <div className="sch-warnings">{rep.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}</div>}
          {view === "script" ? (
            <div className="drift-script"><SqlEditor value={rep.script} readOnly /></div>
          ) : (
            <div className="sch-split drift-split">
              <div className="sch-list" role="listbox" aria-label={t("Differences")}>
                {Object.entries(groups).map(([type, list]) => (
                  <div key={type}>
                    <div className="sch-group">{typeInfo(type)?.label ?? type[0].toUpperCase() + type.slice(1)} <span className="muted">{list.length}</span></div>
                    {list.map((i) => (
                      <div key={(i.schema ?? "") + i.type + i.name} role="option" aria-selected={sel === i} tabIndex={0}
                        className={"sch-item" + (sel === i ? " on" : "")} onClick={() => setSel(i)} onKeyDown={(e) => e.key === "Enter" && setSel(i)}>
                        <span className={"drift-badge " + i.status} aria-label={t(DRIFT_STATUS[i.status])}>{i.status === "missing" ? "−" : i.status === "extra" ? "+" : "≠"}</span>
                        <span className="sch-name">{i.schema ? `${i.schema}.` : ""}{i.name}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div className="sch-detail">
                {sel ? <>
                  <div className="sch-detail-title"><b>{typeInfo(sel.type)?.singular ?? sel.type} {sel.name}</b>
                    <span className={"drift-status " + sel.status}>{t(STATUS_HINT[sel.status])}</span></div>
                  <SideBySide left={sel.baseline_ddl} right={sel.database_ddl}
                    leftTitle={t("Baseline: {name}", { name: rep.baseline.label })} rightTitle={t("Database: {name}", { name: rep.database })} />
                </> : <div className="empty-state">{t("Select an object")}</div>}
              </div>
            </div>
          )}
        </>}
        {!rep.drift && <div className="muted">{t("The schema matches the baseline.")}</div>}
      </>}
    </Section>
  );
}

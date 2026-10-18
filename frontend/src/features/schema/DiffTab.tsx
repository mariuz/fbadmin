import { useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { IconCopy, IconDownload, IconPlay, IconSpinner, IconTerminal, IconUpload } from "../../components/Icons";
import { typeInfo } from "../../types";
import { saveBlob, schemaApi, type DiffItem, type DiffResult, type DiffSide } from "./api";
import { DbPicker, SideBySide } from "./common";
import { N_, t } from "../../i18n";

type SideState = { mode: "db" | "sql"; server_id: string; db_id: string; sql: string; file: string };

const STATUS_LABEL: Record<string, string> = { create: N_("Only in source"), alter: N_("Different"), drop: N_("Only in target") };

function SidePicker(props: { title: string; value: SideState; onChange: (v: SideState) => void; allowSql: boolean }) {
  const v = props.value;
  const readFile = (f: File | undefined) => {
    if (!f) return;
    f.text().then((text) => props.onChange({ ...v, mode: "sql", sql: text, file: f.name }));
  };
  return (
    <div className="sch-side">
      <div className="sch-side-title">{props.title}</div>
      {props.allowSql && (
        <div className="seg" style={{ marginBottom: 6 }}>
          <button className={v.mode === "db" ? "on" : ""} onClick={() => props.onChange({ ...v, mode: "db" })}>{t("Database")}</button>
          <button className={v.mode === "sql" ? "on" : ""} onClick={() => props.onChange({ ...v, mode: "sql" })}>{t("DDL script")}</button>
        </div>
      )}
      {v.mode === "db" ? (
        <DbPicker value={v} onChange={(x) => props.onChange({ ...v, ...x })} />
      ) : (
        <div className="sch-ddl-input">
          <label className="btn small"><IconUpload /> {v.file || t("Choose .sql file…")}
            <input type="file" accept=".sql,.ddl,.txt" style={{ display: "none" }} onChange={(e) => readFile(e.target.files?.[0])} />
          </label>
          <textarea className="text" placeholder={t("…or paste a DDL script. It is run in a temporary database on the server of the other side.")}
            value={v.sql} onChange={(e) => props.onChange({ ...v, sql: e.target.value, file: "" })} />
        </div>
      )}
    </div>
  );
}

export function DiffTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const init = { mode: "db" as const, server_id: tab.serverId ?? app.servers[0]?.id ?? "", db_id: tab.dbId ?? "", sql: "", file: "" };
  const other = app.servers.find((s) => s.id === init.server_id)?.databases.find((d) => d.id !== init.db_id)?.id ?? init.db_id;
  const [source, setSource] = useState<SideState>(init);
  const [target, setTarget] = useState<SideState>({ ...init, db_id: other });
  const [setTerm, setSetTerm] = useState(true);
  const [res, setRes] = useState<DiffResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<DiffItem | null>(null);
  const [view, setView] = useState<"objects" | "script">("objects");
  const [filter, setFilter] = useState<Record<string, boolean>>({ create: true, alter: true, drop: true });

  const side = (s: SideState, other: SideState): DiffSide =>
    s.mode === "db" ? { server_id: s.server_id, db_id: s.db_id } : { server_id: other.server_id || s.server_id, sql: s.sql };

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await schemaApi.diff(side(source, target), side(target, source), setTerm);
      setRes(r);
      setSel(r.items[0] ?? null);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const items = useMemo(() => (res?.items ?? []).filter((i) => filter[i.status]), [res, filter]);
  const groups = useMemo(() => {
    const g: Record<string, DiffItem[]> = {};
    for (const i of items) (g[i.type] ??= []).push(i);
    return g;
  }, [items]);
  const canRun = (source.mode === "db" ? !!source.db_id : !!source.sql.trim()) && (target.mode === "db" ? !!target.db_id : !!target.sql.trim());
  const targetIsDb = target.mode === "db";

  return (
    <div className="sch-col">
      <div className="sch-diff-head">
        <SidePicker title={t("Source (desired schema)")} value={source} onChange={setSource} allowSql />
        <div className="sch-arrow">→</div>
        <SidePicker title={t("Target (database to update)")} value={target} onChange={setTarget} allowSql />
        <div className="sch-diff-actions">
          <label className="check"><input type="checkbox" checked={setTerm} onChange={(e) => setSetTerm(e.target.checked)} /> {t("SET TERM in script")}</label>
          <button className="btn primary" disabled={busy || !canRun} onClick={run}>{busy ? <IconSpinner /> : <IconPlay />} {t("Compare")}</button>
        </div>
      </div>
      {error && <div className="pad err">{error}</div>}
      {res && (
        <>
          <div className="toolbar">
            <div className="seg">
              <button className={view === "objects" ? "on" : ""} onClick={() => setView("objects")}>{t("Differences ({n})", { n: res.items.length })}</button>
              <button className={view === "script" ? "on" : ""} onClick={() => setView("script")}>{t("Migration script")}</button>
            </div>
            {(["create", "alter", "drop"] as const).map((k) => (
              <label key={k} className={"sch-status " + k}>
                <input type="checkbox" checked={filter[k]} onChange={(e) => setFilter({ ...filter, [k]: e.target.checked })} />
                {t(STATUS_LABEL[k])} <b>{res.summary[k] ?? 0}</b>
              </label>
            ))}
            <span className="grow" />
            <button className="btn" onClick={() => saveBlob(new Blob([res.script], { type: "application/sql" }), "migration.sql")}><IconDownload /> {t("Download")}</button>
            <button className="btn" onClick={() => { navigator.clipboard?.writeText(res.script); app.toast(t("Script copied"), "info"); }}><IconCopy /> {t("Copy")}</button>
            <button className="btn" disabled={!targetIsDb} title={targetIsDb ? t("Review and run the script on the target") : t("The target is a DDL script")}
              onClick={() => app.openQueryTool(target.server_id, target.db_id, res.script)}><IconTerminal /> {t("Open in Query Tool (target)")}</button>
          </div>
          {res.warnings.length > 0 && <div className="sch-warnings">{res.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}</div>}
          {view === "script" ? (
            <div className="sch-fill"><SqlEditor value={res.script} readOnly /></div>
          ) : res.items.length === 0 ? (
            <div className="empty-state"><div className="big">{t("The schemas are identical")}</div>
              <div>{t("{source} and {target} have the same objects.", { source: res.source_name, target: res.target_name })}</div></div>
          ) : (
            <div className="sch-split">
              <div className="sch-list">
                {Object.entries(groups).map(([type, list]) => (
                  <div key={type}>
                    <div className="sch-group">{typeInfo(type)?.label ?? type[0].toUpperCase() + type.slice(1)} <span className="muted">{list.length}</span></div>
                    {list.map((i) => (
                      <div key={i.type + i.name} className={"sch-item" + (sel === i ? " on" : "")} onClick={() => setSel(i)}>
                        <span className={"sch-badge " + i.status}>{i.status === "create" ? "+" : i.status === "drop" ? "−" : "≠"}</span>
                        <span className="sch-name">{i.name}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div className="sch-detail">
                {sel ? (
                  <>
                    <div className="sch-detail-title"><b>{typeInfo(sel.type)?.singular ?? sel.type} {sel.name}</b>
                      <span className={"sch-status " + sel.status}>{t(STATUS_LABEL[sel.status])}</span></div>
                    <SideBySide left={sel.source_ddl} right={sel.target_ddl}
                      leftTitle={t("Source: {name}", { name: res.source_name })} rightTitle={t("Target: {name}", { name: res.target_name })} />
                  </>
                ) : <div className="empty-state">{t("Select an object")}</div>}
              </div>
            </div>
          )}
        </>
      )}
      {!res && !error && (
        <div className="empty-state">
          <div className="big">{t("Schema diff")}</div>
          <div>{t("Compare two databases, or a database and a DDL script, and get a migration script")}<br />{t("that makes the target match the source.")}</div>
        </div>
      )}
    </div>
  );
}

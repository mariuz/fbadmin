import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { IconCopy, IconDownload, IconPlay, IconSpinner, IconTerminal } from "../../components/Icons";
import { saveUrl, schemaApi } from "./api";
import { N_, t } from "../../i18n";

const OPTIONS: Array<[string, string]> = [
  ["set_term", N_("SET TERM around PSQL")],
  ["grants", N_("Grants")],
  ["comments", N_("Comments")],
  ["sequence_values", N_("Sequence values")],
];

export function ExtractTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const [opts, setOpts] = useState<Record<string, boolean>>({ set_term: true, grants: true, comments: true, sequence_values: true });
  const [sql, setSql] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setSql((await schemaApi.extract(sid, dbid, opts)).sql);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { run(); }, []);
  const stmts = sql ? (sql.match(/;\s*$|\^\s*$/gm) ?? []).length : 0;

  return (
    <div className="sch-col">
      <div className="toolbar">
        <button className="btn primary" onClick={run} disabled={busy}>{busy ? <IconSpinner /> : <IconPlay />} {t("Extract")}</button>
        {OPTIONS.map(([k, label]) => (
          <label key={k}><input type="checkbox" checked={opts[k]} onChange={(e) => setOpts({ ...opts, [k]: e.target.checked })} /> {t(label)}</label>
        ))}
        <span className="grow" />
        <button className="btn" disabled={!sql} onClick={() => saveUrl(schemaApi.extractUrl(sid, dbid, opts))}><IconDownload /> {t("Download .sql")}</button>
        <button className="btn" disabled={!sql} onClick={() => { navigator.clipboard?.writeText(sql); app.toast(t("Script copied"), "info"); }}><IconCopy /> {t("Copy")}</button>
        <button className="btn" disabled={!sql} onClick={() => app.openQueryTool(sid, dbid, sql)}><IconTerminal /> {t("Open in Query Tool")}</button>
      </div>
      {error && <div className="pad err">{error}</div>}
      <div className="sch-hint muted">
        {t("Objects in dependency order like")} <code>isql -x</code>{t(": domains, sequences, exceptions, tables, keys, PSQL headers with empty bodies, views, PSQL bodies, checks, indices, triggers, grants and comments.")}
        {sql && <> · {t("{lines} lines, ~{n} statements", { lines: sql.split("\n").length.toLocaleString(), n: stmts.toLocaleString() })}</>}
      </div>
      <div className="sch-fill"><SqlEditor value={sql || (busy ? t("-- extracting…") : "")} readOnly /></div>
    </div>
  );
}

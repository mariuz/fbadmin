import { useEffect, useState } from "react";
import { api } from "../../api";
import { errMsg, keys, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { IconPlay, IconSpinner } from "../../components/Icons";
import { schemaApi } from "./api";
import { DbPicker, JobStatus, useJob } from "./common";
import { t } from "../../i18n";

export function CopyTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const [source, setSource] = useState({ server_id: tab.serverId!, db_id: tab.dbId! });
  const srv = app.servers.find((s) => s.id === source.server_id);
  const [target, setTarget] = useState({
    server_id: source.server_id, db_id: srv?.databases.find((d) => d.id !== source.db_id)?.id ?? "",
  });
  const [tables, setTables] = useState<Array<{ name: string; checked: boolean }>>([]);
  const [create, setCreate] = useState(true);
  const [truncate, setTruncate] = useState(false);
  const [batch, setBatch] = useState(1000);
  const [error, setError] = useState<string | null>(null);
  const job = useJob((r) => r.status === "finished" && app.invalidate(keys.folder(target.server_id, target.db_id, "tables")));

  useEffect(() => {
    if (!source.db_id) return;
    const pre = new Set<string>(tab.props?.tables ?? []);
    api.objects(source.server_id, source.db_id, "tables")
      .then((t) => setTables(t.map((x) => ({ name: x.name, checked: pre.size ? pre.has(x.name) : true }))))
      .catch((e) => setError(errMsg(e)));
  }, [source.server_id, source.db_id]);

  const chosen = tables.filter((t) => t.checked).map((t) => t.name);
  const same = source.server_id === target.server_id && source.db_id === target.db_id;
  const start = async () => {
    setError(null);
    try {
      job.start(await schemaApi.copy({ source, target, tables: chosen, create_tables: create, truncate, batch_size: batch }));
    } catch (e) {
      setError(errMsg(e));
    }
  };

  return (
    <div className="sch-col sch-scroll">
      <div className="sch-steps">
        <section>
          <h3>{t("Copy data between databases")}</h3>
          <div className="sch-row">
            <DbPicker label={t("From")} value={source} onChange={setSource} />
            <span className="sch-arrow">→</span>
            <DbPicker label={t("To")} value={target} onChange={setTarget} />
          </div>
          <p className="muted">{t("The databases can be on different servers. Tables are copied parents first (by foreign keys), in batches committed as they go; progress is shown in Processes.")}</p>
        </section>
        <section>
          <h3>{t("Tables")} <span className="muted">{t("{n} of {total}", { n: chosen.length, total: tables.length })}</span></h3>
          <div className="sch-row">
            <button className="btn small" onClick={() => setTables(tables.map((t) => ({ ...t, checked: true })))}>{t("All")}</button>
            <button className="btn small" onClick={() => setTables(tables.map((t) => ({ ...t, checked: false })))}>{t("None")}</button>
          </div>
          <div className="sch-checklist">
            {tables.map((t, i) => (
              <label key={t.name}><input type="checkbox" checked={t.checked}
                onChange={(e) => setTables(tables.map((x, j) => (j === i ? { ...x, checked: e.target.checked } : x)))} /> {t.name}</label>
            ))}
          </div>
        </section>
        <section>
          <h3>{t("Options")}</h3>
          <div className="sch-row">
            <label><input type="checkbox" checked={create} onChange={(e) => setCreate(e.target.checked)} /> {t("Create missing tables (with keys, domains, indices and foreign keys)")}</label>
            <label><input type="checkbox" checked={truncate} onChange={(e) => setTruncate(e.target.checked)} /> {t("Delete existing rows in the target first")}</label>
            <label>{t("Batch size")} <input className="text" type="number" min={1} style={{ width: 90 }} value={batch} onChange={(e) => setBatch(Number(e.target.value) || 1000)} /></label>
          </div>
          <div className="sch-row">
            <button className="btn primary" disabled={!chosen.length || !target.db_id || same || job.running} onClick={start}>
              {job.running ? <IconSpinner /> : <IconPlay />} {t("Copy {n} table(s)", { n: chosen.length })}</button>
            {same && <span className="err">{t("Source and target are the same database")}</span>}
            {error && <span className="err">{error}</span>}
          </div>
          <JobStatus result={job.result} log={job.log} />
        </section>
      </div>
    </div>
  );
}

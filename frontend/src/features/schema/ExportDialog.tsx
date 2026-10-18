import { useEffect, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import { Modal } from "../../components/Modal";
import { postDownload, saveUrl, schemaApi } from "./api";

export function ExportDialog(props: { serverId: string; dbId: string; table?: string }) {
  const app = useApp();
  const [source, setSource] = useState<"table" | "query">("table");
  const [table, setTable] = useState(props.table ?? "");
  const [tables, setTables] = useState<string[]>(props.table ? [props.table] : []);
  const [sql, setSql] = useState("");
  const [format, setFormat] = useState("csv");
  const [delimiter, setDelimiter] = useState(",");
  const [header, setHeader] = useState(true);
  const [tableName, setTableName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = () => app.showDialog(null);

  useEffect(() => {
    Promise.all([api.objects(props.serverId, props.dbId, "tables"), api.objects(props.serverId, props.dbId, "views")])
      .then(([t, v]) => setTables([...t, ...v].map((x) => x.name).sort()))
      .catch(() => undefined);
  }, []);

  const run = async () => {
    setError(null);
    setBusy(true);
    try {
      if (source === "table") {
        if (!table) throw new Error("Choose a table");
        if (format === "sql" && tableName) {
          await postDownload(`/api/servers/${props.serverId}/databases/${props.dbId}/schema/export`,
            { table, format, table_name: tableName }, `${table}.sql`);
        } else {
          // a plain link: the browser streams the file to disk
          saveUrl(schemaApi.exportUrl(props.serverId, props.dbId, table, format, delimiter, header));
        }
      } else {
        await postDownload(`/api/servers/${props.serverId}/databases/${props.dbId}/schema/export`,
          { sql, format, delimiter, header, table_name: tableName || undefined }, `query.${format}`);
      }
      close();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Export data" onClose={close}
      footer={<>
        {error && <span className="err">{error}</span>}
        <button className="btn" onClick={close}>Cancel</button>
        <button className="btn primary" disabled={busy} onClick={run}>Export</button>
      </>}>
      <div className="form">
        <label className="l">Source</label>
        <div className="seg">
          <button className={source === "table" ? "on" : ""} onClick={() => setSource("table")}>Table or view</button>
          <button className={source === "query" ? "on" : ""} onClick={() => setSource("query")}>Query</button>
        </div>
        {source === "table" ? (<>
          <label className="l">Table</label>
          <select className="text" value={table} onChange={(e) => setTable(e.target.value)}>
            <option value="">— choose —</option>
            {tables.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </>) : (<>
          <label className="l">SELECT</label>
          <textarea className="text" rows={5} style={{ fontFamily: "var(--mono)" }} value={sql} onChange={(e) => setSql(e.target.value)} placeholder="SELECT * FROM ..." />
        </>)}
        <label className="l">Format</label>
        <select className="text" value={format} onChange={(e) => setFormat(e.target.value)}>
          <option value="csv">CSV</option><option value="json">JSON</option><option value="xlsx">Excel (XLSX)</option><option value="sql">SQL INSERT statements</option>
        </select>
        {format === "csv" && (<>
          <label className="l">Delimiter</label>
          <select className="text" value={delimiter} onChange={(e) => setDelimiter(e.target.value)}>
            <option value=",">,</option><option value=";">;</option><option value={"\t"}>tab</option><option value="|">|</option>
          </select>
          <span /><label className="check"><input type="checkbox" checked={header} onChange={(e) => setHeader(e.target.checked)} /> Header row</label>
        </>)}
        {format === "sql" && (<>
          <label className="l">INSERT INTO</label>
          <input className="text" value={tableName} placeholder={source === "table" ? table || "table name" : "QUERY_RESULT"} onChange={(e) => setTableName(e.target.value)} />
        </>)}
        <span /><span className="muted">Rows are streamed from the server, so large tables export without loading them in the browser.</span>
      </div>
    </Modal>
  );
}

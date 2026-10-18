import { useEffect, useState } from "react";
import { api } from "../api";
import { errMsg, useApp } from "../context";
import { IconRefresh } from "./Icons";
import { N_, t } from "../i18n";

const ATT_STATE = [N_("idle"), N_("active"), N_("stalled")];
const TX_ISO = [N_("consistency"), N_("concurrency"), N_("read committed (rec ver)"), N_("read committed (no rec ver)"), N_("read committed (read consistency)")];

export function Monitor(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auto, setAuto] = useState(true);

  const load = () => api.monitor(props.serverId, props.dbId).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!auto || !props.visible) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [auto, props.visible]);

  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); app.toast(msg); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn" onClick={load}><IconRefresh /> {t("Refresh")}</button>
        <label className="check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> {t("Auto-refresh (5 s)")}</label>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <div className="pad" style={{ overflow: "auto" }}>
          <div className="cards">
            <div className="card"><div className="k">{t("Attachments")}</div><div className="v">{data.attachments.length}</div></div>
            <div className="card"><div className="k">{t("Transactions")}</div><div className="v">{data.transactions.length}</div></div>
            <div className="card"><div className="k">{t("Running statements")}</div><div className="v">{data.statements.length}</div></div>
            <div className="card"><div className="k">{t("Next tx − OAT")}</div><div className="v">{(data.database.next_transaction - data.database.oat).toLocaleString()}</div></div>
            {data.io && <div className="card"><div className="k">{t("Page reads / writes")}</div><div className="v" style={{ fontSize: 15 }}>{Number(data.io.reads).toLocaleString()} / {Number(data.io.writes).toLocaleString()}</div></div>}
            {data.io && <div className="card"><div className="k">{t("Page fetches")}</div><div className="v" style={{ fontSize: 15 }}>{Number(data.io.fetches).toLocaleString()}</div></div>}
          </div>
          <h3>{t("Attachments")}</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>{t("ID")}</th><th>{t("User")}</th><th>{t("Role")}</th><th>{t("Address")}</th><th>{t("Process")}</th><th>{t("State")}</th><th>{t("Connected")}</th><th>{t("Auth")}</th><th>{t("Charset")}</th><th>{t("Reads")}</th><th>{t("Writes")}</th><th /></tr></thead>
              <tbody>
                {data.attachments.map((a: any) => (
                  <tr key={a.id}>
                    <td>{a.id}{a.is_self ? " (fbadmin)" : ""}</td><td>{a.username}</td><td>{a.role_name}</td><td>{a.remote_address}</td>
                    <td title={a.remote_process}>{(a.remote_process ?? "").split(/[\\/]/).pop()}</td><td>{ATT_STATE[a.state] ? t(ATT_STATE[a.state]) : a.state}</td>
                    <td>{a.connected_at?.replace("T", " ").slice(0, 19)}</td><td>{a.auth_method}</td><td>{a.charset}</td>
                    <td className="num">{a.page_reads}</td><td className="num">{a.page_writes}</td>
                    <td>{!a.is_self && <button className="btn small danger" onClick={() => act(() => api.killAttachment(props.serverId, props.dbId, a.id), t("Attachment {id} disconnected", { id: a.id }))}>{t("Disconnect")}</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>{t("Running statements")}</h3>
          {data.statements.length === 0 ? <div className="muted">{t("No running statements (other than fbadmin's own).")}</div> : (
            <div className="grid-wrap">
              <table className="grid">
                <thead><tr><th>{t("ID")}</th><th>{t("Attachment")}</th><th>{t("Transaction")}</th><th>{t("Started")}</th><th>{t("SQL")}</th><th /></tr></thead>
                <tbody>
                  {data.statements.map((s: any) => (
                    <tr key={s.id}>
                      <td>{s.id}</td><td>{s.attachment_id}</td><td>{s.transaction_id}</td><td>{s.started_at?.replace("T", " ").slice(0, 19)}</td>
                      <td className="mono" title={s.sql_text} style={{ maxWidth: 600 }}>{s.sql_text}</td>
                      <td><button className="btn small danger" onClick={() => act(() => api.cancelStatement(props.serverId, props.dbId, s.id), t("Statement {id} cancelled", { id: s.id }))}>{t("Cancel")}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <h3>{t("Transactions")}</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>{t("ID")}</th><th>{t("Attachment")}</th><th>{t("State")}</th><th>{t("Started")}</th><th>{t("Isolation")}</th><th>{t("Read only")}</th><th>{t("Lock timeout")}</th></tr></thead>
              <tbody>
                {data.transactions.map((tx: any) => (
                  <tr key={tx.id}>
                    <td>{tx.id}</td><td>{tx.attachment_id}</td><td>{tx.state === 1 ? t("active") : t("idle")}</td><td>{tx.started_at?.replace("T", " ").slice(0, 19)}</td>
                    <td>{TX_ISO[tx.isolation] ? t(TX_ISO[tx.isolation]) : tx.isolation}</td><td>{tx.read_only ? t("yes") : ""}</td><td>{tx.lock_timeout === -1 ? t("wait") : tx.lock_timeout === 0 ? t("no wait") : `${tx.lock_timeout}s`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

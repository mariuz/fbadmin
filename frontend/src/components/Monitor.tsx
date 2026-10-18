import { useEffect, useState } from "react";
import { api } from "../api";
import { errMsg, useApp } from "../context";
import { IconRefresh } from "./Icons";

const ATT_STATE = ["idle", "active", "stalled"];
const TX_ISO = ["consistency", "concurrency", "read committed (rec ver)", "read committed (no rec ver)", "read committed (read consistency)"];

export function Monitor(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auto, setAuto] = useState(true);

  const load = () => api.monitor(props.serverId, props.dbId).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!auto || !props.visible) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [auto, props.visible]);

  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); app.toast(msg); load(); } catch (e) { app.toast(errMsg(e), "error"); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="toolbar">
        <button className="btn" onClick={load}><IconRefresh /> Refresh</button>
        <label className="check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Auto-refresh (5 s)</label>
        {error && <span className="err">{error}</span>}
      </div>
      {data && (
        <div className="pad" style={{ overflow: "auto" }}>
          <div className="cards">
            <div className="card"><div className="k">Attachments</div><div className="v">{data.attachments.length}</div></div>
            <div className="card"><div className="k">Transactions</div><div className="v">{data.transactions.length}</div></div>
            <div className="card"><div className="k">Running statements</div><div className="v">{data.statements.length}</div></div>
            <div className="card"><div className="k">Next tx − OAT</div><div className="v">{(data.database.next_transaction - data.database.oat).toLocaleString()}</div></div>
            {data.io && <div className="card"><div className="k">Page reads / writes</div><div className="v" style={{ fontSize: 15 }}>{Number(data.io.reads).toLocaleString()} / {Number(data.io.writes).toLocaleString()}</div></div>}
            {data.io && <div className="card"><div className="k">Page fetches</div><div className="v" style={{ fontSize: 15 }}>{Number(data.io.fetches).toLocaleString()}</div></div>}
          </div>
          <h3>Attachments</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>ID</th><th>User</th><th>Role</th><th>Address</th><th>Process</th><th>State</th><th>Connected</th><th>Auth</th><th>Charset</th><th>Reads</th><th>Writes</th><th /></tr></thead>
              <tbody>
                {data.attachments.map((a: any) => (
                  <tr key={a.id}>
                    <td>{a.id}{a.is_self ? " (fbadmin)" : ""}</td><td>{a.username}</td><td>{a.role_name}</td><td>{a.remote_address}</td>
                    <td title={a.remote_process}>{(a.remote_process ?? "").split(/[\\/]/).pop()}</td><td>{ATT_STATE[a.state] ?? a.state}</td>
                    <td>{a.connected_at?.replace("T", " ").slice(0, 19)}</td><td>{a.auth_method}</td><td>{a.charset}</td>
                    <td className="num">{a.page_reads}</td><td className="num">{a.page_writes}</td>
                    <td>{!a.is_self && <button className="btn small danger" onClick={() => act(() => api.killAttachment(props.serverId, props.dbId, a.id), `Attachment ${a.id} disconnected`)}>Disconnect</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>Running statements</h3>
          {data.statements.length === 0 ? <div className="muted">No running statements (other than fbadmin's own).</div> : (
            <div className="grid-wrap">
              <table className="grid">
                <thead><tr><th>ID</th><th>Attachment</th><th>Transaction</th><th>Started</th><th>SQL</th><th /></tr></thead>
                <tbody>
                  {data.statements.map((s: any) => (
                    <tr key={s.id}>
                      <td>{s.id}</td><td>{s.attachment_id}</td><td>{s.transaction_id}</td><td>{s.started_at?.replace("T", " ").slice(0, 19)}</td>
                      <td className="mono" title={s.sql_text} style={{ maxWidth: 600 }}>{s.sql_text}</td>
                      <td><button className="btn small danger" onClick={() => act(() => api.cancelStatement(props.serverId, props.dbId, s.id), `Statement ${s.id} cancelled`)}>Cancel</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <h3>Transactions</h3>
          <div className="grid-wrap">
            <table className="grid">
              <thead><tr><th>ID</th><th>Attachment</th><th>State</th><th>Started</th><th>Isolation</th><th>Read only</th><th>Lock timeout</th></tr></thead>
              <tbody>
                {data.transactions.map((t: any) => (
                  <tr key={t.id}>
                    <td>{t.id}</td><td>{t.attachment_id}</td><td>{t.state === 1 ? "active" : "idle"}</td><td>{t.started_at?.replace("T", " ").slice(0, 19)}</td>
                    <td>{TX_ISO[t.isolation] ?? t.isolation}</td><td>{t.read_only ? "yes" : ""}</td><td>{t.lock_timeout === -1 ? "wait" : t.lock_timeout === 0 ? "no wait" : `${t.lock_timeout}s`}</td>
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

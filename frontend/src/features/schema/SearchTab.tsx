import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { IconColumn, IconSpinner, IconTable, IconTerminal, TYPE_ICONS } from "../../components/Icons";
import { OBJECT_TYPES, typeInfo, type Selection } from "../../types";
import { schemaApi, type SearchItem } from "./api";

const SCOPES: Array<[string, string]> = [["name", "Name"], ["comment", "Comment"], ["source", "PSQL / view source"]];
const TYPES = ["tables", "views", "columns", "procedures", "functions", "packages", "triggers", "sequences", "domains", "exceptions", "indices", "roles"];

function selectionFor(sid: string, dbid: string, it: SearchItem): Selection {
  if (it.type === "columns") return { kind: "column", serverId: sid, dbId: dbid, otype: it.parent_type ?? "tables", name: it.parent!, column: it.name };
  if (it.package) return { kind: "object", serverId: sid, dbId: dbid, otype: "packages", name: it.package };
  return { kind: "object", serverId: sid, dbId: dbid, otype: it.type, name: it.name };
}

export function SearchTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const [q, setQ] = useState(tab.props?.q ?? "");
  const [scopes, setScopes] = useState<string[]>(["name", "comment", "source"]);
  const [type, setType] = useState("");
  const [items, setItems] = useState<SearchItem[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [current, setCurrent] = useState<SearchItem | null>(null);
  const [ddl, setDdl] = useState("");
  const input = useRef<HTMLInputElement>(null);

  const run = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await schemaApi.search(sid, dbid, q.trim(), scopes, type ? [type] : []);
      setItems(r.items);
      setTruncated(r.truncated);
      setCurrent(null);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { input.current?.focus(); if (q) run(); }, []);

  const open = async (it: SearchItem) => {
    setCurrent(it);
    const sel = selectionFor(sid, dbid, it);
    app.select(sel);
    setDdl("-- loading…");
    try {
      setDdl((await api.object(sid, dbid, sel.otype!, sel.name!)).ddl);
    } catch (e) {
      setDdl(`-- ${errMsg(e)}`);
    }
  };
  const highlight = (text: string) => {
    const i = text.toLowerCase().indexOf(q.trim().toLowerCase());
    if (i < 0 || !q.trim()) return text;
    return <>{text.slice(0, i)}<mark>{text.slice(i, i + q.trim().length)}</mark>{text.slice(i + q.trim().length)}</>;
  };

  return (
    <div className="sch-col">
      <div className="toolbar">
        <input ref={input} className="text" style={{ width: 280 }} placeholder="Search objects…" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
        <button className="btn primary" onClick={run} disabled={busy || !q.trim()}>{busy ? <IconSpinner /> : null} Search</button>
        {SCOPES.map(([k, label]) => (
          <label key={k}><input type="checkbox" checked={scopes.includes(k)}
            onChange={(e) => setScopes(e.target.checked ? [...scopes, k] : scopes.filter((s) => s !== k))} /> {label}</label>
        ))}
        <select className="text" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">All types</option>
          {TYPES.map((t) => <option key={t} value={t}>{typeInfo(t)?.label ?? "Columns"}</option>)}
        </select>
        <span className="grow" />
        {items && <span className="muted">{items.length}{truncated ? "+" : ""} result(s)</span>}
      </div>
      {error && <div className="pad err">{error}</div>}
      <div className="sch-split">
        <div className="sch-results">
          {items === null ? <div className="empty-state"><div className="big">Search objects</div><div>by name, comment or PSQL / view source</div></div>
            : items.length === 0 ? <div className="empty-state">No objects found.</div> : (
              <table className="grid sch-search-grid">
                <thead><tr><th>Type</th><th>Name</th><th>Parent</th><th>Found in</th><th>Match</th></tr></thead>
                <tbody>
                  {items.map((it, i) => {
                    const Icon = it.type === "columns" ? IconColumn : TYPE_ICONS[it.type] ?? IconTable;
                    return (
                      <tr key={i} className={current === it ? "sel" : ""} onClick={() => open(it)} title="Select in the object explorer">
                        <td><Icon size={13} /> {OBJECT_TYPES.find((t) => t.type === it.type)?.singular ?? "Column"}</td>
                        <td><a href="#" onClick={(e) => e.preventDefault()}>{it.match === "name" ? highlight(it.name) : it.name}</a></td>
                        <td>{it.parent ?? it.package ?? ""}</td>
                        <td>{it.match}{it.line ? ` (line ${it.line})` : ""}</td>
                        <td className="sch-snippet">{it.snippet ? highlight(it.snippet) : ""}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
        </div>
        {current && (
          <div className="sch-detail" style={{ flex: "none", width: "40%" }}>
            <div className="toolbar">
              <b>{current.package ?? (current.type === "columns" ? current.parent : current.name)}</b>
              <span className="grow" />
              <button className="btn" onClick={() => app.openQueryTool(sid, dbid, ddl)}><IconTerminal /> Open in Query Tool</button>
            </div>
            <div className="sch-fill"><SqlEditor value={ddl} readOnly /></div>
          </div>
        )}
      </div>
    </div>
  );
}

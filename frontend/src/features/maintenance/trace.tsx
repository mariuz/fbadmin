import { useEffect, useMemo, useRef, useState } from "react";
import { errMsg, useApp } from "../../context";
import { IconDownload, IconPlay, IconRefresh, IconTrash, IconX } from "../../components/Icons";
import { browserDownload, fmtTime, mapi, type TraceEvent, type TraceSummary, type TraceTemplate } from "./api";
import { useBusy } from "./common";

const MAX_CLIENT_EVENTS = 5000;

export function TracePanel(props: { serverId: string; dbId?: string; visible: boolean }) {
  const app = useApp();
  const srv = app.servers.find((s) => s.id === props.serverId);
  const [templates, setTemplates] = useState<TraceTemplate[]>([]);
  const [form, setForm] = useState({ template: "slow_queries", database_id: props.dbId ?? "", time_threshold: "1000", include_filter: "", exclude_filter: "", config: "" });
  const [preview, setPreview] = useState("");
  const [sessions, setSessions] = useState<{ local: TraceSummary[]; server: Array<{ id: number; name: string | null; user: string | null; date: string | null; flags: string[] }> }>({ local: [], server: [] });
  const [current, setCurrent] = useState<TraceSummary | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [minMs, setMinMs] = useState("");
  const [follow, setFollow] = useState(true);
  const [sel, setSel] = useState<TraceEvent | null>(null);
  const lastSeq = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const { busy, error, run } = useBusy();

  const loadSessions = () => mapi.traceSessions(props.serverId).then(setSessions).catch((e) => app.toast(errMsg(e), "error"));
  useEffect(() => {
    mapi.traceTemplates(props.serverId).then(setTemplates).catch(() => undefined);
    loadSessions();
  }, [props.serverId]);
  const tpl = templates.find((t) => t.id === form.template);
  const body = () => ({
    template: form.template, database_id: form.database_id || null,
    time_threshold: tpl && "time_threshold" in tpl.params && form.time_threshold !== "" ? Number(form.time_threshold) : null,
    include_filter: form.include_filter || null, exclude_filter: form.exclude_filter || null,
    config: form.template === "custom" ? form.config : null,
  });
  useEffect(() => {
    if (form.template === "custom") return;
    const t = setTimeout(() => mapi.tracePreview(props.serverId, body()).then((r) => setPreview(r.config)).catch(() => undefined), 200);
    return () => clearTimeout(t);
  }, [form, templates]);
  useEffect(() => {
    if (tpl && "time_threshold" in tpl.params) setForm((f) => ({ ...f, time_threshold: String(tpl.params.time_threshold) }));
  }, [form.template, templates.length]);

  // live polling of the selected session
  useEffect(() => {
    if (!current || !props.visible) return;
    let live = true;
    const tick = async () => {
      try {
        const r = await mapi.traceEvents(props.serverId, current.id, lastSeq.current);
        if (!live) return;
        if (r.events.length) {
          lastSeq.current = r.events[r.events.length - 1].seq;
          setEvents((ev) => [...ev, ...r.events].slice(-MAX_CLIENT_EVENTS));
        }
        setCurrent(r.session);
      } catch { /* ignore */ }
    };
    tick();
    const t = setInterval(tick, 1000);
    return () => { live = false; clearInterval(t); };
  }, [current?.id, current?.status === "running", props.visible]);
  useEffect(() => {
    if (follow && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [events.length, follow]);

  const open = (s: TraceSummary) => {
    lastSeq.current = 0;
    setEvents([]);
    setSel(null);
    setCurrent(s);
  };
  const types = useMemo(() => [...new Set(events.map((e) => e.type.split(" ")[0]))].sort(), [events]);
  const shown = useMemo(() => {
    const q = search.toLowerCase();
    const ms = Number(minMs) || 0;
    return events.filter((e) => (!typeFilter || e.type.startsWith(typeFilter)) && (!ms || (e.duration_ms ?? 0) >= ms)
      && (!q || e.text.toLowerCase().includes(q)));
  }, [events, search, typeFilter, minMs]);
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  return (
    <div className="mt">
      <div className="mt-nav" style={{ width: 330, padding: 12 }}>
        <b>New trace session</b>
        <div className="form" style={{ gridTemplateColumns: "90px 1fr", marginTop: 8 }}>
          <label className="l">Template</label>
          <select className="text" value={form.template} onChange={set("template")}>
            {templates.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
          <span />
          <span className="muted" style={{ fontSize: 12 }}>{tpl?.description}</span>
          <label className="l">Database</label>
          <select className="text" value={form.database_id} onChange={set("database_id")}>
            <option value="">(all databases)</option>
            {srv?.databases.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
          {tpl && "time_threshold" in tpl.params && <>
            <label className="l">Threshold ms</label>
            <input className="text" type="number" min={0} value={form.time_threshold} onChange={set("time_threshold")} />
          </>}
          {form.template !== "custom" && <>
            <label className="l">Include SQL</label>
            <input className="text" placeholder="SIMILAR TO, e.g. %ORDERS%" value={form.include_filter} onChange={set("include_filter")} />
            <label className="l">Exclude SQL</label>
            <input className="text" placeholder="SIMILAR TO" value={form.exclude_filter} onChange={set("exclude_filter")} />
          </>}
        </div>
        <textarea className="text mono" rows={9} style={{ width: "100%", marginTop: 8 }} readOnly={form.template !== "custom"}
          value={form.template === "custom" ? form.config : preview}
          placeholder={"database = %[\\\\/]my.fdb\n{\n  enabled = true\n  log_statement_finish = true\n}"}
          onChange={set("config")} onFocus={() => form.template !== "custom" && !form.config && setForm({ ...form, config: preview })} />
        <div className="mt-row" style={{ marginTop: 6 }}>
          <button className="btn primary" disabled={busy} onClick={() => run(async () => {
            const s = await mapi.traceStart(props.serverId, body());
            open(s);
            loadSessions();
          }, "Trace session started")}><IconPlay size={13} /> Start trace</button>
          {form.template !== "custom" && <button className="btn small" onClick={() => setForm({ ...form, template: "custom", config: preview })}>Edit as custom</button>}
        </div>
        {error && <div className="err" style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{error}</div>}
        <div className="mt-row" style={{ marginTop: 16 }}><b>Sessions</b><span style={{ flex: 1 }} /><button className="btn icon small" onClick={loadSessions}><IconRefresh size={12} /></button></div>
        {sessions.local.length === 0 && <div className="muted" style={{ fontSize: 12 }}>None started from fbadmin.</div>}
        {sessions.local.map((s) => (
          <div key={s.id} className="history-item" style={{ padding: "6px 4px", background: current?.id === s.id ? "var(--select)" : undefined }} onClick={() => open(s)}>
            <span className={"pill " + (s.status === "running" ? "running" : s.status === "failed" ? "failed" : "finished")}>{s.status}</span>{" "}
            <b>{s.name}</b> <span className="muted">#{s.session_id ?? "?"}</span>
            <div className="muted" style={{ fontSize: 11 }}>{s.database ?? "all databases"} · {fmtTime(s.started_at)} · {s.event_count} events</div>
          </div>
        ))}
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>On the server</div>
        {sessions.server.map((s) => (
          <div key={s.id} className="mt-row" style={{ fontSize: 12, padding: "3px 0" }}>
            <span>#{s.id} {s.name ?? "(unnamed)"} <span className="muted">{s.user} · {s.flags.join(", ")}</span></span>
            <span style={{ flex: 1 }} />
            <button className="btn small" title="Stop this server trace session" onClick={() => run(async () => { await mapi.traceStopServer(props.serverId, s.id); loadSessions(); })}><IconX size={11} /></button>
          </div>
        ))}
      </div>
      <div className="trace" style={{ flex: 1, minWidth: 0 }}>
        {!current ? <div className="empty-state"><div className="big">Trace API</div><div>Start a session from a template, or pick one on the left to watch its events live.</div></div> : <>
          <div className="toolbar">
            <span className={"pill " + (current.status === "running" ? "running" : current.status === "failed" ? "failed" : "finished")}>{current.status}</span>
            <b>{current.name}</b><span className="muted">session #{current.session_id} · {current.event_count} events</span>
            {current.error && <span className="err" title={current.error}>{current.error}</span>}
            <span className="sep" />
            <input className="text" placeholder="Search events" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 180 }} />
            <select className="text" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="">all event types</option>
              {types.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <input className="text" type="number" placeholder="≥ ms" style={{ width: 70 }} value={minMs} onChange={(e) => setMinMs(e.target.value)} />
            <label><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow</label>
            <span className="grow" />
            {current.status === "running" && <button className="btn stop" disabled={busy} onClick={() => run(async () => { setCurrent(await mapi.traceStop(props.serverId, current.id)); loadSessions(); }, "Trace stopped")}>Stop</button>}
            <button className="btn" onClick={() => browserDownload(mapi.traceDownloadUrl(props.serverId, current.id), `trace-${current.id}.log`)}><IconDownload size={13} /> Save log</button>
            <button className="btn" onClick={() => { setEvents([]); setSel(null); }}>Clear view</button>
            {current.status !== "running" && <button className="btn danger" onClick={() => run(async () => { await mapi.traceDelete(props.serverId, current.id); setCurrent(null); loadSessions(); })}><IconTrash size={13} /></button>}
          </div>
          <div className="trace-body">
            <div className="trace-list" ref={listRef} onScroll={(e) => {
              const el = e.currentTarget;
              const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
              if (atBottom !== follow) setFollow(atBottom);
            }}>
              <table className="grid" style={{ width: "100%" }}>
                <thead><tr><th>#</th><th>Time</th><th>Event</th><th>ms</th><th>Att.</th><th>User</th><th>SQL / details</th></tr></thead>
                <tbody>
                  {shown.map((e) => (
                    <tr key={e.seq} className={sel?.seq === e.seq ? "sel" : ""} onClick={() => setSel(e)} style={{ cursor: "pointer" }}>
                      <td className="num">{e.seq}</td><td>{e.time.slice(11, 23)}</td>
                      <td className={e.type.includes("ERROR") ? "err" : ""}>{e.type}</td>
                      <td className="num">{e.duration_ms ?? ""}</td><td className="num">{e.attachment ?? ""}</td><td>{e.user ?? ""}</td>
                      <td className="mono" style={{ maxWidth: 520 }}>{(e.sql ?? e.text.split("\n").slice(1).join(" ")).replace(/\s+/g, " ").slice(0, 200)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {shown.length === 0 && <div className="pad muted">{events.length ? "No events match the filter." : current.status === "running" ? "Waiting for events…" : "No events."}</div>}
            </div>
            {sel && (
              <div className="trace-detail">
                <div className="mt-row"><b>{sel.type}</b><span className="muted">{sel.time}</span><span style={{ flex: 1 }} /><button className="btn icon small" onClick={() => setSel(null)}><IconX size={12} /></button></div>
                {sel.sql && <><h3>SQL</h3><pre className="log">{sel.sql}</pre></>}
                {sel.plan && <><h3>Plan</h3><pre className="log">{sel.plan}</pre></>}
                <h3>Event</h3><pre className="log">{sel.text}</pre>
              </div>
            )}
          </div>
        </>}
      </div>
    </div>
  );
}

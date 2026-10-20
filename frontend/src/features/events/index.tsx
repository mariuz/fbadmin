/**
 * Milestone 1.2 — event monitor: register POST_EVENT names on a database, watch the events arrive
 * live with counts, suggest names found in PSQL sources, and post test events.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import "./events.css";
import { registerMenu, registerTabKind } from "../../registry";
import { errMsg, useApp, type AppApi } from "../../context";
import { N_, t } from "../../i18n";
import type { Selection } from "../../types";
import { IconActivity, IconPlay, IconPlus, IconRefresh, IconX } from "../../components/Icons";
import { Section, useBusy } from "../maintenance/common";
import { eapi, type EventArrival, type EventChanges, type EventSession, type EventSuggestions } from "./api";

const MAX_CLIENT_LOG = 2000;
// session states, shown through t(status)
N_("starting"); N_("listening"); N_("error"); N_("expired"); N_("closed");
const SOURCE_LABEL: Record<string, string> = {
  trigger: N_("trigger"), procedure: N_("procedure"), function: N_("function"), package: N_("package body"),
};

const storeKey = (sid: string, dbid: string) => `fbadmin.events.${sid}.${dbid}`;
function savedNames(sid: string, dbid: string): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(storeKey(sid, dbid)) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch { return []; }
}
function saveNames(sid: string, dbid: string, names: string[]) {
  try { localStorage.setItem(storeKey(sid, dbid), JSON.stringify(names)); } catch { /* private mode */ }
}

/** "A, B C" -> ["A", "B", "C"]; quotes keep spaces: "MY EVENT". */
export function splitNames(text: string): string[] {
  const out: string[] = [];
  const re = /"([^"]+)"|'([^']+)'|([^\s,;]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) out.push((m[1] ?? m[2] ?? m[3]).trim());
  return out.filter(Boolean);
}

const fmtClock = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");
const fmtClockMs = (ts: number) => {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString() + "." + String(d.getMilliseconds()).padStart(3, "0");
};

function Sparkline(props: { values: number[]; label: string }) {
  const w = 90, h = 18;
  const max = Math.max(1, ...props.values);
  const n = props.values.length;
  const pts = props.values.map((v, i) => `${((i + 0.5) * w) / n},${h - 1 - (v / max) * (h - 3)}`).join(" ");
  return (
    <svg className="ev-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={props.label}>
      <line x1={0} y1={h - 0.5} x2={w} y2={h - 0.5} className="base" />
      <polyline points={pts} fill="none" />
    </svg>
  );
}

export function EventMonitorPanel(props: { serverId: string; dbId: string; visible: boolean; names?: string[] }) {
  const app = useApp();
  const srv = app.servers.find((s) => s.id === props.serverId);
  const db = srv?.databases.find((d) => d.id === props.dbId);
  const readOnly = !!srv?.read_only;
  const [session, setSession] = useState<EventSession | null>(null);
  const [log, setLog] = useState<EventArrival[]>([]);
  const [openError, setOpenError] = useState<{ message: string; code?: string } | null>(null);
  const [addText, setAddText] = useState("");
  const [postName, setPostName] = useState("");
  const [postCount, setPostCount] = useState("1");
  const [sugg, setSugg] = useState<EventSuggestions | null>(null);
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const lastSeq = useRef(0);
  const sessionId = useRef<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const { busy, error, run } = useBusy();
  const ids = useId();

  const apply = (r: EventChanges) => {
    setSession(r.session);
    if (r.log.length) {
      lastSeq.current = Math.max(lastSeq.current, r.log[r.log.length - 1].seq);
      setLog((l) => [...l, ...r.log.filter((e) => !l.length || e.seq > l[l.length - 1].seq)].slice(-MAX_CLIENT_LOG));
    }
    saveNames(props.serverId, props.dbId, r.session.names.map((n) => n.name));
  };

  const start = async () => {
    setOpenError(null);
    const names = [...new Set([...(props.names ?? []), ...savedNames(props.serverId, props.dbId)])];
    try {
      const r = await eapi.open(props.serverId, props.dbId, names);
      sessionId.current = r.session.id;
      lastSeq.current = 0;
      setLog([]);
      apply(r);
    } catch (e) {
      setOpenError({ message: errMsg(e), code: (e as { code?: string }).code });
    }
  };

  // one backend session per tab: opened on mount, closed when the tab closes
  useEffect(() => {
    start();
    return () => {
      const id = sessionId.current;
      sessionId.current = null;
      if (id) eapi.close(props.serverId, props.dbId, id).catch(() => undefined);
    };
  }, [props.serverId, props.dbId]);

  // add names passed when the tab is opened again (e.g. "Monitor" from a suggestion elsewhere)
  useEffect(() => {
    if (session && props.names?.length) {
      const missing = props.names.filter((n) => !session.names.some((x) => x.name === n));
      if (missing.length) eapi.register(props.serverId, props.dbId, session.id, missing).then(apply).catch(() => undefined);
    }
  }, [props.names?.join("\n"), session?.id]);

  // live polling; slower while the tab is hidden so the session does not expire
  const live = session?.status === "listening";
  useEffect(() => {
    if (!session) return;
    let alive = true;
    const tick = async () => {
      const id = sessionId.current;
      if (!id) return;
      try {
        const r = await eapi.poll(props.serverId, props.dbId, id, lastSeq.current);
        if (alive) apply(r);
      } catch (e) {
        if (alive && (e as { status?: number }).status === 404) {
          setSession((s) => (s ? { ...s, status: "expired" } : s));
        }
      }
    };
    const timer = setInterval(tick, props.visible ? 1000 : 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [session?.id, live, props.visible]);

  useEffect(() => {
    if (follow && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log.length, follow]);

  const loadSuggestions = () => run(async () => setSugg(await eapi.suggestions(props.serverId, props.dbId)));
  useEffect(() => { loadSuggestions(); }, [props.serverId, props.dbId]);

  const registered = new Set(session?.names.map((n) => n.name) ?? []);
  /** ``typed``: the text box content being submitted; the box is cleared afterwards only if it still
   * holds that text (a registration from a suggestion must not wipe what the user typed meanwhile). */
  const add = (names: string[], typed?: string) => {
    if (!session) return;
    const fresh = names.filter((n) => !registered.has(n));
    if (!fresh.length) return;
    run(async () => {
      apply(await eapi.register(props.serverId, props.dbId, session.id, fresh));
      if (typed !== undefined) setAddText((cur) => (cur === typed ? "" : cur));
    });
  };
  const remove = (name: string) => session && run(async () => apply(await eapi.unregister(props.serverId, props.dbId, session.id, [name])));
  const reconnect = () => session && run(async () => {
    try {
      apply(await eapi.reconnect(props.serverId, props.dbId, session.id));
    } catch (e) {
      if ((e as { status?: number }).status === 404) { sessionId.current = null; await start(); } else throw e;
    }
  });
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? log.filter((e) => e.name.toLowerCase().includes(q)) : log;
  }, [log, filter]);
  const total = session?.names.reduce((a, n) => a + n.count, 0) ?? 0;
  const windowMin = Math.round((session?.rate_window ?? 300) / 60);

  if (!db) return <div className="pad muted">{t("The database is no longer registered.")}</div>;
  return (
    <div className="mt ev">
      <div className="ev-side">
        <h2 className="ev-title">{t("Event monitor")} <span className="muted">{db.name}</span></h2>
        {openError && (
          <div className="err ev-openerr" role="alert">
            <b>{openError.code === "aux_unreachable" ? t("Cannot open the event connection") : openError.code === "aux_busy" ? t("The server's event port is busy") : t("Cannot start the event monitor")}</b>
            <div>{openError.message}</div>
            <button className="btn small" onClick={() => start()}><IconRefresh size={12} /> {t("Try again")}</button>
          </div>
        )}
        <Section title={t("Registered events")} wide
          hint={t("Counts are per name since it was registered (or the counters were reset). Events posted in a transaction arrive when it commits.")}>
          <form className="mt-row" onSubmit={(e) => { e.preventDefault(); add(splitNames(addText), addText); }}>
            <label htmlFor={ids + "add"} className="sr-only">{t("Event names to register")}</label>
            <input id={ids + "add"} className="text" style={{ flex: 1, minWidth: 160 }} placeholder={t("EVENT_NAME, OTHER_EVENT …")}
              value={addText} onChange={(e) => setAddText(e.target.value)} disabled={!session} />
            <button className="btn primary small" type="submit" disabled={!session || busy || !splitNames(addText).length}><IconPlus size={12} /> {t("Register")}</button>
          </form>
          {session && session.names.length > 0 ? (
            <table className="grid ev-names">
              <thead><tr><th>{t("Event")}</th><th className="num">{t("Count")}</th><th>{t("Last")}</th><th>{t("Last {n} min", { n: windowMin })}</th><th><span className="sr-only">{t("Actions")}</span></th></tr></thead>
              <tbody>
                {session.names.map((n) => (
                  <tr key={n.name} className={n.last_at && session.now - n.last_at < 2 ? "ev-flash" : ""}>
                    <td className="mono">{n.name}{!n.listening && session.status === "listening" && <span className="muted"> · {t("registering…")}</span>}</td>
                    <td className="num" data-testid={"count-" + n.name}>{n.count}</td>
                    <td>{fmtClock(n.last_at)}</td>
                    <td><Sparkline values={n.rate} label={t("{count} in the last {n} min", { count: n.rate.reduce((a, b) => a + b, 0), n: windowMin })} /></td>
                    <td><button className="btn icon small" aria-label={t("Unregister {name}", { name: n.name })} title={t("Unregister")} onClick={() => remove(n.name)}><IconX size={11} /></button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="muted small ev-empty">{session ? t("No event names registered yet.") : ""}</div>}
        </Section>

        <Section title={t("Posted in PSQL")} wide
          right={<button className="btn small" aria-label={t("Rescan sources")} title={t("Rescan sources")} onClick={loadSuggestions}><IconRefresh size={12} /></button>}
          hint={t("POST_EVENT calls with a literal name found in triggers, procedures, functions and package bodies.")}>
          {sugg && sugg.names.length === 0 && <div className="muted small">{t("No POST_EVENT with a literal name found.")}</div>}
          {sugg && sugg.names.length > 0 && <>
            <ul className="ev-sugg">
              {sugg.names.map((s) => (
                <li key={s.name}>
                  <button className="btn small" disabled={!session || registered.has(s.name)} onClick={() => add([s.name])}
                    aria-label={t("Register {name}", { name: s.name })}><IconPlus size={11} /></button>
                  <span className="mono">{s.name}</span>
                  <span className="muted small">{s.sources.map((x) => `${t(SOURCE_LABEL[x.type] ?? x.type)} ${x.parent && x.type !== "trigger" ? x.parent + "." : ""}${x.name}${x.type === "trigger" && x.parent ? " (" + x.parent + ")" : ""}`).join(", ")}</span>
                </li>
              ))}
            </ul>
            <button className="btn small" disabled={!session || sugg.names.every((s) => registered.has(s.name))} onClick={() => add(sugg.names.map((s) => s.name))}>{t("Register all")}</button>
          </>}
          {sugg && sugg.dynamic.length > 0 && (
            <div className="muted small" style={{ marginTop: 8 }}>
              {t("Also posted with a computed name:")} {sugg.dynamic.map((d) => `${d.name} (POST_EVENT ${d.expression})`).join("; ")}
            </div>
          )}
        </Section>

        <Section title={t("Post a test event")} wide
          hint={readOnly ? t("This server is registered as read-only: posting events is disabled.") : t("Runs EXECUTE BLOCK … POST_EVENT in its own transaction and commits it.")}>
          <form className="mt-row" onSubmit={(e) => {
            e.preventDefault();
            const name = postName.trim();
            if (name) run(async () => { await eapi.post(props.serverId, props.dbId, name, Number(postCount) || 1); }, t("Event {name} posted", { name }));
          }}>
            <label htmlFor={ids + "pname"} className="sr-only">{t("Event name to post")}</label>
            <input id={ids + "pname"} className="text" style={{ flex: 1, minWidth: 140 }} placeholder={t("Event name")} list={ids + "plist"}
              value={postName} onChange={(e) => setPostName(e.target.value)} disabled={readOnly} />
            <datalist id={ids + "plist"}>{session?.names.map((n) => <option key={n.name} value={n.name} />)}</datalist>
            <label htmlFor={ids + "pcount"} className="muted small">{t("times")}</label>
            <input id={ids + "pcount"} className="text" type="number" min={1} max={100} style={{ width: 64 }} value={postCount}
              onChange={(e) => setPostCount(e.target.value)} disabled={readOnly} />
            <button className="btn small" type="submit" disabled={readOnly || busy || !postName.trim()}><IconPlay size={11} /> {t("Post")}</button>
          </form>
        </Section>
        {error && <div className="err" role="alert" style={{ whiteSpace: "pre-wrap" }}>{error}</div>}
      </div>

      <div className="trace ev-main">
        <div className="toolbar">
          {session && <span className={"pill " + (session.status === "listening" ? "running" : session.status === "error" ? "failed" : "finished")}>{t(session.status)}</span>}
          <b>{t("Live log")}</b>
          {session && <span className="muted">{t("{count} events · {n} notifications", { count: total, n: session.notifications })}</span>}
          {session?.error && <span className="err" title={session.error}>{session.error}</span>}
          <span className="sep" />
          <label htmlFor={ids + "filter"} className="sr-only">{t("Filter the log by event name")}</label>
          <input id={ids + "filter"} className="text" placeholder={t("Filter by name")} value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 150 }} />
          <label><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> {t("follow")}</label>
          <span className="grow" />
          {session && session.status !== "listening" && <button className="btn primary" disabled={busy} onClick={reconnect}><IconRefresh size={12} /> {t("Reconnect")}</button>}
          <button className="btn" disabled={!session || busy} onClick={() => session && run(async () => { apply(await eapi.reset(props.serverId, props.dbId, session.id)); setLog([]); }, t("Counters reset"))}>{t("Reset counters")}</button>
          <button className="btn" onClick={() => setLog([])}>{t("Clear log")}</button>
        </div>
        <div className="trace-list" ref={logRef} tabIndex={0} aria-label={t("Live log")} onScroll={(e) => {
          const el = e.currentTarget;
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
          if (atBottom !== follow) setFollow(atBottom);
        }}>
          <table className="grid ev-log" style={{ width: "100%" }}>
            <thead><tr><th>#</th><th>{t("Time")}</th><th>{t("Event")}</th><th className="num">{t("Count")}</th><th className="num">{t("Total")}</th></tr></thead>
            <tbody>
              {shown.map((e) => (
                <tr key={e.seq}>
                  <td className="num">{e.seq}</td><td>{fmtClockMs(e.time)}</td><td className="mono">{e.name}</td>
                  <td className="num">{e.count > 1 ? "×" + e.count : e.count}</td><td className="num">{e.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.length === 0 && (
            <div className="pad muted">{log.length ? t("No events match the filter.") : session?.status === "listening"
              ? (session.names.length ? t("Waiting for events…") : t("Register event names to start listening."))
              : openError ? t("Not connected.") : t("Connecting…")}</div>
          )}
        </div>
      </div>
    </div>
  );
}

export function openEventMonitor(app: AppApi, sid: string, dbid: string, names?: string[]) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
  app.openTab({ kind: "event-monitor", title: t("{name} · events", { name: db?.name ?? "db" }), serverId: sid, dbId: dbid, props: { names } });
}

registerTabKind("event-monitor", {
  icon: <IconActivity size={13} />,
  render: (tab, visible) => <EventMonitorPanel serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} names={tab.props?.names} key={tab.id} />,
  same: (a, b) => a.serverId === b.serverId && a.dbId === b.dbId,
});

registerMenu((app: AppApi, sel: Selection) => {
  if (sel.kind !== "database" || !sel.serverId || !sel.dbId) return [];
  return [{ label: t("Event monitor"), icon: <IconActivity />, onClick: () => openEventMonitor(app, sel.serverId, sel.dbId!) }];
});

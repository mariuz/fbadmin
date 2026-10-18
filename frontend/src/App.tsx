import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, setPasswordHandler } from "./api";
import { AppCtx, errMsg, selectionKey, type AppApi, type WorkTab } from "./context";
import type { ObjectDetail, Selection, Server } from "./types";
import { Tree } from "./components/Tree";
import { PasswordDialog, ServerDialog } from "./components/dialogs";
import { Dashboard, DependenciesPanel, FolderList, PropertiesPanel, SqlPanel } from "./components/ObjectPanels";
// Code-split: these views load on first use (see vite.config.ts for the vendor chunks).
const QueryTool = lazy(() => import("./components/QueryTool").then((m) => ({ default: m.QueryTool })));
const DataView = lazy(() => import("./components/DataView").then((m) => ({ default: m.DataView })));
const Monitor = lazy(() => import("./components/Monitor").then((m) => ({ default: m.Monitor })));
const JobsPanel = lazy(() => import("./components/Jobs").then((m) => ({ default: m.JobsPanel })));
const ServerLog = lazy(() => import("./components/Jobs").then((m) => ({ default: m.ServerLog })));
import { getObjectTabs, getTabKind, getTopbarItems } from "./registry";
import { Flame, IconActivity, IconMoon, IconPlus, IconRefresh, IconSun, IconTable, IconTerminal, IconX } from "./components/Icons";

const FIXED_TABS = [
  { id: "dashboard", title: "Dashboard" },
  { id: "properties", title: "Properties" },
  { id: "sql", title: "SQL" },
  { id: "deps", title: "Dependencies" },
];

let tabSeq = 0;

export default function App() {
  const [servers, setServers] = useState<Server[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [tabs, setTabs] = useState<WorkTab[]>([]);
  const [active, setActive] = useState("dashboard");
  const [dialog, setDialog] = useState<ReactNode | null>(null);
  const [toasts, setToasts] = useState<Array<{ id: number; msg: string; kind: string }>>([]);
  const [versions, setVersions] = useState<Record<string, number>>({});
  const [detail, setDetail] = useState<ObjectDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [runningJobs, setRunningJobs] = useState(0);
  const [theme, setTheme] = useState<string>(() => {
    try { return localStorage.getItem("fbadmin.theme") || ""; } catch { return ""; }
  });
  const [sidebarW, setSidebarW] = useState(300);
  const serversRef = useRef(servers);
  serversRef.current = servers;

  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("fbadmin.theme", theme); } catch { /* ignore */ }
  }, [theme]);

  const toast = useCallback((msg: string, kind: "ok" | "error" | "info" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 8000 : 3500);
  }, []);

  const reloadServers = useCallback(async () => {
    const s = await api.servers();
    setServers(s);
    return s;
  }, []);

  const invalidate = useCallback((key: string) => setVersions((v) => ({ ...v, [key]: (v[key] ?? 0) + 1 })), []);

  const openTab = useCallback((t: Omit<WorkTab, "id"> & { id?: string }) => {
    // reuse an existing data/monitor/log tab for the same object
    let id = t.id;
    setTabs((cur) => {
      if (t.kind !== "query") {
        const reg = getTabKind(t.kind);
        const existing = cur.find((x) => x.kind === t.kind && (reg?.same
          ? reg.same(x, t)
          : x.serverId === t.serverId && x.dbId === t.dbId && x.name === t.name));
        if (existing) {
          id = existing.id;
          return cur;
        }
      }
      id = id ?? `tab${++tabSeq}`;
      return [...cur, { ...t, id } as WorkTab];
    });
    setTimeout(() => setActive(id!), 0);
  }, []);

  const closeTab = useCallback((id: string) => {
    setTabs((cur) => {
      const idx = cur.findIndex((t) => t.id === id);
      const next = cur.filter((t) => t.id !== id);
      setActive((a) => (a === id ? next[Math.max(0, idx - 1)]?.id ?? "dashboard" : a));
      return next;
    });
  }, []);

  const openQueryTool = useCallback((serverId: string, dbId: string, sql?: string, autorun?: boolean) => {
    const srv = serversRef.current.find((s) => s.id === serverId);
    const db = srv?.databases.find((d) => d.id === dbId);
    openTab({ kind: "query", title: `${db?.name ?? "query"}`, serverId, dbId, initialSql: sql, autorun });
  }, [openTab]);

  const jobStarted = useCallback(() => setRunningJobs((n) => n + 1), []);

  const app: AppApi = useMemo(() => ({
    servers, reloadServers, selection, select: setSelection, openTab, closeTab, showDialog: setDialog, toast,
    invalidate, versions, openQueryTool, jobStarted,
  }), [servers, selection, versions]);

  // password prompt for servers without a saved password
  const appRef = useRef(app);
  appRef.current = app;
  useEffect(() => {
    setPasswordHandler((serverId) => new Promise<boolean>((resolve) => {
      const srv = serversRef.current.find((s) => s.id === serverId);
      if (!srv) return resolve(false);
      setDialog(<PasswordDialog server={srv} resolve={resolve} />);
    }));
    reloadServers().catch((e) => toast(errMsg(e), "error"));
  }, []);

  // poll running jobs count for the badge
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const js = await api.jobs();
        setRunningJobs(js.filter((j) => j.status === "running").length);
      } catch { /* backend down */ }
    }, 3000);
    return () => clearInterval(t);
  }, []);

  // load object detail for properties/sql/deps
  const objKey = selection && (selection.kind === "object" || selection.kind === "column")
    ? `${selection.serverId}:${selection.dbId}:${selection.otype}:${selection.name}` : "";
  const objVersion = selection?.otype ? versions[`f:${selection.serverId}:${selection.dbId}:${selection.otype}`] ?? 0 : 0;
  useEffect(() => {
    setDetail(null);
    setDetailError(null);
    if (!objKey || !selection) return;
    let cancelled = false;
    api.object(selection.serverId, selection.dbId!, selection.otype!, selection.name!)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setDetailError(errMsg(e)));
    return () => { cancelled = true; };
  }, [objKey, objVersion, selection]);

  // when selecting something, show a sensible fixed tab
  const lastSel = useRef("");
  useEffect(() => {
    const k = selectionKey(selection);
    if (k === lastSel.current) return;
    lastSel.current = k;
    const extra = getObjectTabs().find((t) => t.id === active);
    if (extra && !extra.applies(selection)) { setActive("properties"); return; }
    if (!FIXED_TABS.some((t) => t.id === active)) return;
    if (selection?.kind === "object" || selection?.kind === "column") {
      if (active === "dashboard") setActive("properties");
    } else if (active !== "dashboard") setActive("dashboard");
  }, [selection]);

  const startResize = (e: React.MouseEvent) => {
    const x0 = e.clientX, w0 = sidebarW;
    const move = (ev: MouseEvent) => setSidebarW(Math.min(Math.max(180, w0 + ev.clientX - x0), window.innerWidth * 0.6));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const selDb = selection?.dbId ? selection : null;
  const selIsRelation = selection?.kind === "object" && ["tables", "views", "system_tables"].includes(selection.otype ?? "");

  const renderFixed = (id: string) => {
    if (id === "dashboard") {
      if (selection?.kind === "folder") return <FolderList sel={selection} />;
      if (selection?.kind === "object" || selection?.kind === "column") return <PropertiesPanel sel={selection} detail={detail} error={detailError} />;
      return <Dashboard sel={selection} />;
    }
    if (id === "properties") return <PropertiesPanel sel={selection} detail={detail} error={detailError} />;
    if (id === "sql") return <SqlPanel sel={selection} detail={detail} error={detailError} />;
    return <DependenciesPanel sel={selection} detail={detail} />;
  };

  return (
    <AppCtx.Provider value={app}>
      <div className="app">
        <div className="topbar">
          <div className="brand"><Flame /> fbadmin <small>Firebird administration</small></div>
          <button className="tb" onClick={() => setDialog(<ServerDialog />)}><IconPlus /> Add server</button>
          <button className="tb" disabled={!selDb} style={{ opacity: selDb ? 1 : 0.5 }} onClick={() => selDb && openQueryTool(selDb.serverId, selDb.dbId!)}><IconTerminal /> Query Tool</button>
          <button className="tb" disabled={!selIsRelation} style={{ opacity: selIsRelation ? 1 : 0.5 }}
            onClick={() => selection && selIsRelation && openTab({ kind: "data", title: selection.name!, serverId: selection.serverId, dbId: selection.dbId, otype: selection.otype, name: selection.name })}>
            <IconTable /> View data
          </button>
          {getTopbarItems().map((it) => <span key={it.id}>{it.render(app)}</span>)}
          <span className="spacer" />
          <button className="tb" onClick={() => openTab({ kind: "jobs", title: "Processes" })}>
            <IconActivity /> Processes {runningJobs > 0 && <span className="badge">{runningJobs}</span>}
          </button>
          <button className="tb" title="Toggle theme" onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "" : "dark")}>
            {theme === "dark" ? <IconMoon /> : theme === "light" ? <IconSun /> : <><IconSun /><span style={{ fontSize: 11 }}>auto</span></>}
          </button>
        </div>
        <div className="main">
          <div className="sidebar" style={{ width: sidebarW }}>
            <div className="sidebar-head">
              <span className="grow">Object Explorer</span>
              <button className="btn icon small" title="Reload servers" onClick={() => reloadServers()}><IconRefresh size={13} /></button>
              <button className="btn icon small" title="Register server" onClick={() => setDialog(<ServerDialog />)}><IconPlus size={13} /></button>
            </div>
            <Tree />
          </div>
          <div className="splitter-v" onMouseDown={startResize} />
          <div className="workspace">
            <div className="tabs">
              {FIXED_TABS.map((t) => (
                <div key={t.id} className={"tab" + (active === t.id ? " active" : "")} onClick={() => setActive(t.id)}>
                  <span className="t">{t.title}</span>
                </div>
              ))}
              {getObjectTabs().filter((t) => t.applies(selection)).map((t) => (
                <div key={t.id} className={"tab" + (active === t.id ? " active" : "")} onClick={() => setActive(t.id)}>
                  <span className="t">{t.title}</span>
                </div>
              ))}
              {tabs.map((t) => (
                <div key={t.id} className={"tab" + (active === t.id ? " active" : "")} onClick={() => setActive(t.id)}
                  onAuxClick={(e) => e.button === 1 && closeTab(t.id)} title={t.title}>
                  {t.kind === "query" ? <IconTerminal size={13} /> : t.kind === "data" ? <IconTable size={13} /> : getTabKind(t.kind)?.icon ?? <IconActivity size={13} />}
                  <span className="t">{t.title}</span>
                  <button className="x" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}><IconX size={12} /></button>
                </div>
              ))}
            </div>
            <div className="tab-body">
              {FIXED_TABS.map((t) => (
                <div key={t.id} className={"tab-pane" + (active === t.id ? "" : " hidden")}>{active === t.id && renderFixed(t.id)}</div>
              ))}
              {getObjectTabs().map((t) => (
                <div key={t.id} className={"tab-pane" + (active === t.id ? "" : " hidden")}>
                  {active === t.id && selection && t.applies(selection) && t.render(selection, detail)}
                </div>
              ))}
              {tabs.map((t) => (
                <div key={t.id} className={"tab-pane" + (active === t.id ? "" : " hidden")} style={{ overflow: "hidden" }}>
                  <Suspense fallback={<div className="empty">Loading…</div>}>
                  {t.kind === "query" && <QueryTool serverId={t.serverId!} dbId={t.dbId!} initialSql={t.initialSql} autorun={t.autorun} visible={active === t.id} />}
                  {t.kind === "data" && <DataView serverId={t.serverId!} dbId={t.dbId!} otype={t.otype!} name={t.name!} showFilter={t.initialSql === "filter"} />}
                  {t.kind === "monitor" && <Monitor serverId={t.serverId!} dbId={t.dbId!} visible={active === t.id} />}
                  {t.kind === "jobs" && <JobsPanel visible={active === t.id} />}
                  {t.kind === "serverlog" && <ServerLog serverId={t.serverId!} />}
                  {t.kind === "serverinfo" && <Dashboard sel={{ kind: "server", serverId: t.serverId! }} />}
                  {getTabKind(t.kind)?.render(t, active === t.id)}
                  </Suspense>
                </div>
              ))}
            </div>
          </div>
        </div>
        {dialog}
        <div className="toasts">
          {toasts.map((t) => <div key={t.id} className={"toast " + t.kind}>{t.msg}</div>)}
        </div>
      </div>
    </AppCtx.Provider>
  );
}

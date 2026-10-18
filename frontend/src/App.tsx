import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { activeSchemaOf, api, setActiveSchema, setPasswordHandler } from "./api";
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
import { setPrefs, usePrefs } from "./prefs";
import { currentLanguage, N_, t } from "./i18n";
import { PreferencesDialog } from "./components/Preferences";
import { Flame, IconActivity, IconMoon, IconPlus, IconRefresh, IconSun, IconTable, IconTerminal, IconX } from "./components/Icons";

const FIXED_TABS = [
  { id: "dashboard", title: N_("Dashboard") },
  { id: "properties", title: N_("Properties") },
  { id: "sql", title: N_("SQL") },
  { id: "deps", title: N_("Dependencies") },
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
  const [sidebarW, setSidebarW] = useState(300);
  const serversRef = useRef(servers);
  serversRef.current = servers;
  const prefs = usePrefs();
  const theme = prefs.theme;
  const setTheme = (th: string) => setPrefs({ theme: th as typeof prefs.theme });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }, [theme]);
  useEffect(() => {
    document.documentElement.lang = currentLanguage();
  }, [prefs.language]);

  const toast = useCallback((msg: string, kind: "ok" | "error" | "info" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { id, msg, kind }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), kind === "error" ? 8000 : 3500);
  }, []);

  const reloadServers = useCallback(async () => {
    const s = await api.servers();
    setServers(s);
    return s;
  }, []);

  const invalidate = useCallback((key: string) => setVersions((v) => ({ ...v, [key]: (v[key] ?? 0) + 1 })), []);

  const openTab = useCallback((t0: Omit<WorkTab, "id"> & { id?: string }) => {
    // tabs remember the Firebird 6 schema they were opened in
    const tab = { ...t0, schema: t0.schema ?? activeSchemaOf(t0.serverId, t0.dbId) };
    // reuse an existing data/monitor/log tab for the same object
    let id = tab.id;
    setTabs((cur) => {
      if (tab.kind !== "query") {
        const reg = getTabKind(tab.kind);
        const existing = cur.find((x) => x.kind === tab.kind && (reg?.same
          ? reg.same(x, tab)
          : x.serverId === tab.serverId && x.dbId === tab.dbId && x.name === tab.name && x.schema === tab.schema));
        if (existing) {
          id = existing.id;
          return cur;
        }
      }
      id = id ?? `tab${++tabSeq}`;
      return [...cur, { ...tab, id } as WorkTab];
    });
    setTimeout(() => setActive(id!), 0);
  }, []);

  const closeTab = useCallback((id: string) => {
    setTabs((cur) => {
      const idx = cur.findIndex((tb) => tb.id === id);
      const next = cur.filter((tb) => tb.id !== id);
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

  // selecting a node of a Firebird 6 schema makes that schema active for its database
  const select = useCallback((sel: Selection | null) => {
    if (sel?.dbId && sel.schema) setActiveSchema(sel.serverId, sel.dbId, sel.schema);
    setSelection(sel);
  }, []);
  // ... and so does switching to a tab opened in a schema
  useEffect(() => {
    const tab = tabs.find((x) => x.id === active);
    if (tab?.serverId && tab.dbId && tab.schema) setActiveSchema(tab.serverId, tab.dbId, tab.schema);
  }, [active, tabs]);

  const app: AppApi = useMemo(() => ({
    servers, reloadServers, selection, select, openTab, closeTab, showDialog: setDialog, toast,
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
    const timer = setInterval(async () => {
      try {
        const js = await api.jobs();
        setRunningJobs(js.filter((j) => j.status === "running").length);
      } catch { /* backend down */ }
    }, 3000);
    return () => clearInterval(timer);
  }, []);

  // load object detail for properties/sql/deps
  const objKey = selection && (selection.kind === "object" || selection.kind === "column")
    ? `${selection.serverId}:${selection.dbId}:${selection.schema ?? ""}:${selection.otype}:${selection.name}` : "";
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
    const extra = getObjectTabs().find((ot) => ot.id === active);
    if (extra && !extra.applies(selection)) { setActive("properties"); return; }
    if (!FIXED_TABS.some((ft) => ft.id === active)) return;
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
          <div className="brand"><Flame /> fbadmin <small>{t("Firebird administration")}</small></div>
          <button className="tb" onClick={() => setDialog(<ServerDialog />)}><IconPlus /> {t("Add server")}</button>
          <button className="tb" disabled={!selDb} style={{ opacity: selDb ? 1 : 0.5 }} onClick={() => selDb && openQueryTool(selDb.serverId, selDb.dbId!)}><IconTerminal /> {t("Query Tool")}</button>
          <button className="tb" disabled={!selIsRelation} style={{ opacity: selIsRelation ? 1 : 0.5 }}
            onClick={() => selection && selIsRelation && openTab({ kind: "data", title: selection.name!, serverId: selection.serverId, dbId: selection.dbId, otype: selection.otype, name: selection.name })}>
            <IconTable /> {t("View data")}
          </button>
          {getTopbarItems().map((it) => <span key={it.id}>{it.render(app)}</span>)}
          <span className="spacer" />
          <button className="tb" onClick={() => openTab({ kind: "jobs", title: N_("Processes") })}>
            <IconActivity /> {t("Processes")} {runningJobs > 0 && <span className="badge">{runningJobs}</span>}
          </button>
          <button className="tb" title={t("Preferences")} aria-label={t("Preferences")} onClick={() => setDialog(<PreferencesDialog />)}>⚙</button>
          <button className="tb" title={t("Toggle theme")} aria-label={t("Toggle theme")} onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "contrast" : theme === "contrast" ? "" : "dark")}>
            {theme === "dark" ? <IconMoon /> : theme === "light" ? <IconSun /> : theme === "contrast" ? <span style={{ fontWeight: 700 }}>◐</span> : <><IconSun /><span style={{ fontSize: 11 }}>{t("auto")}</span></>}
          </button>
        </div>
        <div className="main">
          <div className="sidebar" style={{ width: sidebarW }}>
            <div className="sidebar-head">
              <span className="grow">{t("Object Explorer")}</span>
              <button className="btn icon small" title={t("Reload servers")} onClick={() => reloadServers()}><IconRefresh size={13} /></button>
              <button className="btn icon small" title={t("Register server")} onClick={() => setDialog(<ServerDialog />)}><IconPlus size={13} /></button>
            </div>
            <Tree />
          </div>
          <div className="splitter-v" onMouseDown={startResize} />
          <div className="workspace">
            <div className="tabs">
              {FIXED_TABS.map((ft) => (
                <div key={ft.id} className={"tab" + (active === ft.id ? " active" : "")} onClick={() => setActive(ft.id)}>
                  <span className="t">{t(ft.title)}</span>
                </div>
              ))}
              {getObjectTabs().filter((ot) => ot.applies(selection)).map((ot) => (
                <div key={ot.id} className={"tab" + (active === ot.id ? " active" : "")} onClick={() => setActive(ot.id)}>
                  <span className="t">{t(ot.title)}</span>
                </div>
              ))}
              {tabs.map((tab) => {
                // the Processes tab has a static (N_-marked) title; other titles carry data
                const title = tab.kind === "jobs" ? t(tab.title) : tab.title;
                return (
                <div key={tab.id} className={"tab" + (active === tab.id ? " active" : "")} onClick={() => setActive(tab.id)}
                  onAuxClick={(e) => e.button === 1 && closeTab(tab.id)} title={title}>
                  {tab.kind === "query" ? <IconTerminal size={13} /> : tab.kind === "data" ? <IconTable size={13} /> : getTabKind(tab.kind)?.icon ?? <IconActivity size={13} />}
                  <span className="t">{title}</span>
                  <button className="x" onClick={(e) => { e.stopPropagation(); closeTab(tab.id); }}><IconX size={12} /></button>
                </div>
                );
              })}
            </div>
            <div className="tab-body">
              {FIXED_TABS.map((ft) => (
                <div key={ft.id} className={"tab-pane" + (active === ft.id ? "" : " hidden")}>{active === ft.id && renderFixed(ft.id)}</div>
              ))}
              {getObjectTabs().map((ot) => (
                <div key={ot.id} className={"tab-pane" + (active === ot.id ? "" : " hidden")}>
                  {active === ot.id && selection && ot.applies(selection) && ot.render(selection, detail)}
                </div>
              ))}
              {tabs.map((tab) => (
                <div key={tab.id} className={"tab-pane" + (active === tab.id ? "" : " hidden")} style={{ overflow: "hidden" }}>
                  <Suspense fallback={<div className="empty">{t("Loading…")}</div>}>
                  {tab.kind === "query" && <QueryTool serverId={tab.serverId!} dbId={tab.dbId!} schema={tab.schema} initialSql={tab.initialSql} autorun={tab.autorun} visible={active === tab.id} />}
                  {tab.kind === "data" && <DataView serverId={tab.serverId!} dbId={tab.dbId!} otype={tab.otype!} name={tab.name!} showFilter={tab.initialSql === "filter"} />}
                  {tab.kind === "monitor" && <Monitor serverId={tab.serverId!} dbId={tab.dbId!} visible={active === tab.id} />}
                  {tab.kind === "jobs" && <JobsPanel visible={active === tab.id} />}
                  {tab.kind === "serverlog" && <ServerLog serverId={tab.serverId!} />}
                  {tab.kind === "serverinfo" && <Dashboard sel={{ kind: "server", serverId: tab.serverId! }} />}
                  {getTabKind(tab.kind)?.render(tab, active === tab.id)}
                  </Suspense>
                </div>
              ))}
            </div>
          </div>
        </div>
        {dialog}
        <div className="toasts">
          {toasts.map((ts) => <div key={ts.id} className={"toast " + ts.kind}>{ts.msg}</div>)}
        </div>
      </div>
    </AppCtx.Provider>
  );
}

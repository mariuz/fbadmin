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
import { currentLayout, useLayout } from "./features/mobile/responsive";
import { TopbarOverflow, useNavDrawer } from "./features/mobile/Shell";

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
  // tablets start with a narrower Object Explorer
  const [sidebarW, setSidebarW] = useState(() => (currentLayout() === "tablet" ? 260 : 300));
  // phones and tablets (features/mobile): drawer / collapsible sidebar, topbar overflow menu
  const layout = useLayout();
  const compact = layout !== "desktop";
  const nav = useNavDrawer(layout);
  const closeDrawer = nav.closeDrawer;
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
          // e.g. "Index advisor" on an open performance tab: pass the new props to the tab
          if (tab.props && JSON.stringify(tab.props) !== JSON.stringify(existing.props)) {
            return cur.map((x) => (x.id === existing.id ? { ...x, props: { ...x.props, ...tab.props } } : x));
          }
          return cur;
        }
      }
      id = id ?? `tab${++tabSeq}`;
      return [...cur, { ...tab, id } as WorkTab];
    });
    setTimeout(() => setActive(id!), 0);
    closeDrawer(); // phone: show the tab that was just opened
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
    reloadServers()
      .then((s) => {
        // phones land on the multi-server overview (features/overview)
        if (s.length && currentLayout() === "phone" && getTabKind("overview")) openTab({ kind: "overview", title: t("Overview") });
      })
      .catch((e) => toast(errMsg(e), "error"));
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

  // small screens: keep the active tab visible in the scrolling tab strip
  useEffect(() => {
    if (compact) document.getElementById(`tab-${active}`)?.closest(".tab")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [active, compact]);

  // phone: tapping an item in the Object Explorer opens it (closes the drawer, shows its dashboard
  // or properties); the twisty only expands. Keyboard navigation (synthetic clicks) keeps it open.
  const onNavClick = (e: React.MouseEvent) => {
    if (!nav.phone || e.detail === 0) return;
    const el = e.target as Element;
    if (!el.closest(".tree-row") || el.closest(".twisty, .tree-more")) return;
    nav.setDrawer(false);
    setActive("dashboard");
  };

  // --- tab bar accessibility (WAI-ARIA tabs pattern)
  const visibleTabIds = [
    ...FIXED_TABS.map((x) => x.id),
    ...getObjectTabs().filter((ot) => ot.applies(selection)).map((ot) => ot.id),
    ...tabs.map((x) => x.id),
  ];
  const tabProps = (id: string) => ({
    role: "tab", id: `tab-${id}`, "aria-selected": active === id, "aria-controls": `pane-${id}`,
    tabIndex: active === id ? 0 : -1, onClick: () => setActive(id),
  });
  const paneProps = (id: string) => ({ role: "tabpanel", id: `pane-${id}`, "aria-labelledby": `tab-${id}` });
  const onTabsKey = (e: React.KeyboardEvent) => {
    const i = visibleTabIds.indexOf(active);
    let next: string | undefined;
    if (e.key === "ArrowRight") next = visibleTabIds[(i + 1) % visibleTabIds.length];
    else if (e.key === "ArrowLeft") next = visibleTabIds[(i - 1 + visibleTabIds.length) % visibleTabIds.length];
    else if (e.key === "Home") next = visibleTabIds[0];
    else if (e.key === "End") next = visibleTabIds[visibleTabIds.length - 1];
    else if (e.key === "Delete" && tabs.some((x) => x.id === active)) { closeTab(active); e.preventDefault(); return; }
    if (!next) return;
    e.preventDefault();
    setActive(next);
    setTimeout(() => document.getElementById(`tab-${next}`)?.focus(), 0);
  };

  // topbar buttons (a row on desktop; partly in the "⋯" sheet on phones and tablets)
  const addServerBtn = <button className="tb" onClick={() => setDialog(<ServerDialog />)}><IconPlus /> {t("Add server")}</button>;
  const queryToolBtn = <button className="tb" disabled={!selDb} style={{ opacity: selDb ? 1 : 0.5 }} onClick={() => selDb && openQueryTool(selDb.serverId, selDb.dbId!)}><IconTerminal /> {t("Query Tool")}</button>;
  const viewDataBtn = (
    <button className="tb" disabled={!selIsRelation} style={{ opacity: selIsRelation ? 1 : 0.5 }}
      onClick={() => selection && selIsRelation && openTab({ kind: "data", title: selection.name!, serverId: selection.serverId, dbId: selection.dbId, otype: selection.otype, name: selection.name })}>
      <IconTable /> {t("View data")}
    </button>
  );
  const processesBtn = (
    <button className="tb" onClick={() => openTab({ kind: "jobs", title: N_("Processes") })}>
      <IconActivity /> {t("Processes")} {runningJobs > 0 && <span className="badge">{runningJobs}</span>}
    </button>
  );
  const prefsBtn = (labelled: boolean) => labelled
    ? <button className="tb" onClick={() => setDialog(<PreferencesDialog />)}><span aria-hidden="true">⚙</span> {t("Preferences")}</button>
    : <button className="tb" title={t("Preferences")} aria-label={t("Preferences")} onClick={() => setDialog(<PreferencesDialog />)}>⚙</button>;
  const themeBtn = (labelled: boolean) => (
    <button className="tb" title={t("Toggle theme")} aria-label={labelled ? undefined : t("Toggle theme")} onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "contrast" : theme === "contrast" ? "" : "dark")}>
      {theme === "dark" ? <IconMoon /> : theme === "light" ? <IconSun /> : theme === "contrast" ? <span style={{ fontWeight: 700 }}>◐</span> : <><IconSun /><span style={{ fontSize: 11 }}>{t("auto")}</span></>}
      {labelled && <> {t("Toggle theme")}</>}
    </button>
  );

  return (
    <AppCtx.Provider value={app}>
      <div className="app">
        <header className="topbar">
          {compact && (
            <button ref={nav.toggleRef} className="tb nav-toggle" aria-controls="object-explorer" aria-expanded={nav.open}
              title={t("Object Explorer")} aria-label={t("Object Explorer")} onClick={nav.toggle}>
              <span aria-hidden="true">☰</span>
            </button>
          )}
          <div className="brand"><Flame /> fbadmin <small>{t("Firebird administration")}</small></div>
          {!compact ? (
            <>
              {addServerBtn}
              {queryToolBtn}
              {viewDataBtn}
              {getTopbarItems().map((it) => <span key={it.id}>{it.render(app)}</span>)}
              <span className="spacer" />
              {processesBtn}
              {prefsBtn(false)}
              {themeBtn(false)}
            </>
          ) : (
            <>
              {/* the overview stays in the bar; on tablets the Query Tool too */}
              {getTopbarItems().filter((it) => it.id === "overview").map((it) => <span key={it.id}>{it.render(app)}</span>)}
              {layout === "tablet" && queryToolBtn}
              <span className="spacer" />
              <TopbarOverflow badge={runningJobs > 0 ? <span className="badge">{runningJobs}</span> : null}>
                {layout === "phone" && queryToolBtn}
                {viewDataBtn}
                {addServerBtn}
                {getTopbarItems().filter((it) => it.id !== "overview").map((it) => <span key={it.id}>{it.render(app)}</span>)}
                {processesBtn}
                {prefsBtn(true)}
                {themeBtn(true)}
              </TopbarOverflow>
            </>
          )}
        </header>
        <div className="main">
          {nav.phone && nav.drawer && <div className="drawer-back" aria-hidden="true" onClick={nav.closeDrawer} />}
          <nav ref={nav.navRef} id="object-explorer" style={{ width: layout === "phone" ? undefined : sidebarW }} aria-label={t("Object Explorer")}
            className={"sidebar" + (nav.phone ? " drawer" + (nav.drawer ? " open" : "") : "") + (layout === "tablet" && !nav.open ? " collapsed" : "")}
            onClick={onNavClick} {...nav.navTouch}>
            <div className="sidebar-head">
              <span className="grow">{t("Object Explorer")}</span>
              <button className="btn icon small" title={t("Reload servers")} onClick={() => reloadServers()}><IconRefresh size={13} /></button>
              <button className="btn icon small" title={t("Register server")} onClick={() => setDialog(<ServerDialog />)}><IconPlus size={13} /></button>
              {nav.phone && <button className="btn icon small" title={t("Close")} aria-label={t("Close")} onClick={nav.closeDrawer}><IconX size={13} /></button>}
            </div>
            <Tree />
          </nav>
          {!(layout === "tablet" && !nav.open) && <div className="splitter-v" onMouseDown={startResize} />}
          <main className="workspace">
            {/* WAI-ARIA tabs: arrows/Home/End move between tabs, Delete closes a work tab */}
            <div className="tabs" role="tablist" aria-label={t("Panels")} onKeyDown={onTabsKey}>
              {FIXED_TABS.map((ft) => (
                <div key={ft.id} className={"tab" + (active === ft.id ? " active" : "")} role="presentation" onClick={() => setActive(ft.id)}>
                  <span className="t" {...tabProps(ft.id)}>{t(ft.title)}</span>
                </div>
              ))}
              {getObjectTabs().filter((ot) => ot.applies(selection)).map((ot) => (
                <div key={ot.id} className={"tab" + (active === ot.id ? " active" : "")} role="presentation" onClick={() => setActive(ot.id)}>
                  <span className="t" {...tabProps(ot.id)}>{t(ot.title)}</span>
                </div>
              ))}
              {tabs.map((tab) => {
                // the Processes tab has a static (N_-marked) title; other titles carry data
                const title = tab.kind === "jobs" ? t(tab.title) : tab.title;
                return (
                <div key={tab.id} className={"tab" + (active === tab.id ? " active" : "")} role="presentation" onClick={() => setActive(tab.id)}
                  onAuxClick={(e) => e.button === 1 && closeTab(tab.id)} title={title}>
                  {tab.kind === "query" ? <IconTerminal size={13} /> : tab.kind === "data" ? <IconTable size={13} /> : getTabKind(tab.kind)?.icon ?? <IconActivity size={13} />}
                  <span className="t" {...tabProps(tab.id)} aria-keyshortcuts="Delete">{title}</span>
                  {/* mouse target only: from the keyboard, Delete closes the focused tab */}
                  <span className="x" aria-hidden="true" title={t("Close {title}", { title })} onClick={(e) => { e.stopPropagation(); closeTab(tab.id); }}><IconX size={12} /></span>
                </div>
                );
              })}
            </div>
            <div className="tab-body">
              {FIXED_TABS.map((ft) => (
                <div key={ft.id} className={"tab-pane" + (active === ft.id ? "" : " hidden")} {...paneProps(ft.id)}>{active === ft.id && renderFixed(ft.id)}</div>
              ))}
              {getObjectTabs().map((ot) => (
                <div key={ot.id} className={"tab-pane" + (active === ot.id ? "" : " hidden")} {...paneProps(ot.id)}>
                  {active === ot.id && selection && ot.applies(selection) && ot.render(selection, detail)}
                </div>
              ))}
              {tabs.map((tab) => (
                <div key={tab.id} className={"tab-pane" + (active === tab.id ? "" : " hidden")} style={{ overflow: "hidden" }} {...paneProps(tab.id)}>
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
          </main>
        </div>
        {dialog}
        <div className="toasts" role="status" aria-live="polite">
          {toasts.map((ts) => <div key={ts.id} className={"toast " + ts.kind} role={ts.kind === "error" ? "alert" : undefined}>{ts.msg}</div>)}
        </div>
      </div>
    </AppCtx.Provider>
  );
}

/**
 * Extension points. Features live in src/features/<name>/ and register themselves here
 * (imported once from src/features/index.ts), so adding a feature rarely touches core files.
 */
import type { ReactNode } from "react";
import type { AppApi, WorkTab } from "./context";
import type { ObjectDetail, Selection } from "./types";
import type { MenuItem } from "./components/ContextMenu";

// --- workspace tabs (opened with app.openTab({ kind, ... })) -----------------
export interface TabKind {
  /** Small icon shown in the tab header. */
  icon?: ReactNode;
  render: (tab: WorkTab, visible: boolean) => ReactNode;
  /** Reuse an existing tab when this returns true (default: same kind + server + db + name). */
  same?: (a: WorkTab, b: Omit<WorkTab, "id">) => boolean;
}
const tabKinds: Record<string, TabKind> = {};
export function registerTabKind(kind: string, t: TabKind) {
  tabKinds[kind] = t;
}
export function getTabKind(kind: string): TabKind | undefined {
  return tabKinds[kind];
}

// --- context menu items -------------------------------------------------------
export type MenuContributor = (app: AppApi, sel: Selection) => MenuItem[];
const menuContributors: MenuContributor[] = [];
export function registerMenu(fn: MenuContributor) {
  menuContributors.push(fn);
}
export function contributedMenuItems(app: AppApi, sel: Selection): MenuItem[] {
  const out: MenuItem[] = [];
  for (const fn of menuContributors) {
    try {
      const items = fn(app, sel);
      // a separator between contributors ends the previous contributor's titled section
      if (items.length && out.length) out.push({ separator: true });
      out.push(...items);
    } catch (e) {
      console.error("menu contributor failed", e);
    }
  }
  return out;
}

/** Filters run on the finished context menu (e.g. hide items that do not apply to a kind of server). */
export type MenuFilter = (app: AppApi, sel: Selection, items: MenuItem[]) => MenuItem[];
const menuFilters: MenuFilter[] = [];
export function registerMenuFilter(fn: MenuFilter) {
  menuFilters.push(fn);
}
export function applyMenuFilters(app: AppApi, sel: Selection, items: MenuItem[]): MenuItem[] {
  return menuFilters.reduce((acc, fn) => {
    try {
      return fn(app, sel, acc);
    } catch (e) {
      console.error("menu filter failed", e);
      return acc;
    }
  }, items);
}

// --- top bar buttons ------------------------------------------------------------
export type TopbarItem = { id: string; render: (app: AppApi) => ReactNode };
const topbarItems: TopbarItem[] = [];
export function registerTopbar(item: TopbarItem) {
  topbarItems.push(item);
}
export function getTopbarItems() {
  return topbarItems;
}

// --- extra fixed tabs next to Dashboard / Properties / SQL / Dependencies -------
export interface ObjectTab {
  id: string;
  /** English title marked with N_(); translated with t() where the tab is rendered. */
  title: string;
  /** Show the tab only for selections where this returns true. */
  applies: (sel: Selection | null) => boolean;
  render: (sel: Selection, detail: ObjectDetail | null) => ReactNode;
}
const objectTabs: ObjectTab[] = [];
export function registerObjectTab(t: ObjectTab) {
  objectTabs.push(t);
}
export function getObjectTabs() {
  return objectTabs;
}

// --- dashboard sections (database dashboard, below the built-in content) ------
export type DashboardSection = { id: string; render: (sel: Selection) => ReactNode };
const dashboardSections: DashboardSection[] = [];
export function registerDashboardSection(s: DashboardSection) {
  dashboardSections.push(s);
}
export function getDashboardSections() {
  return dashboardSections;
}

// --- extra Object Explorer nodes ------------------------------------------------
/** Extra child nodes under a database (after the built-in folders). */
export interface TreeNodesProvider {
  parent: "database";
  build: (serverId: string, dbId: string) => Promise<import("./components/Tree").NodeSpec[]>;
}
const treeNodes: TreeNodesProvider[] = [];
export function registerTreeNodes(p: TreeNodesProvider) {
  treeNodes.push(p);
}
export function getTreeNodes(parent: "database") {
  return treeNodes.filter((t) => t.parent === parent);
}

// --- Query Tool toolbar buttons -------------------------------------------------
export interface QueryToolContext {
  serverId: string;
  dbId: string;
  /** The selected text, or the whole editor content when nothing is selected. */
  selectionOrAll: () => string;
  /** The whole editor content. */
  getSql: () => string;
  /** Replace the editor content. */
  setSql: (sql: string) => void;
}
export type QueryToolAction = { id: string; render: (ctx: QueryToolContext) => ReactNode };
const queryToolActions: QueryToolAction[] = [];
export function registerQueryToolAction(a: QueryToolAction) {
  queryToolActions.push(a);
}
export function getQueryToolActions() {
  return queryToolActions;
}

// --- Query Tool result views (shown next to the grid: Grid | Chart | …) ----------
export interface ResultViewContext {
  columns: import("./types").ColumnInfo[];
  rows: unknown[][];
  /** the open cursor has more rows */
  hasMore: boolean;
  loadingMore: boolean;
  /** fetch up to ``rows`` more rows from the open cursor */
  loadMore: (rows?: number) => Promise<{ fetched: number; hasMore: boolean }>;
}
export interface ResultView {
  id: string;
  /** English title marked with N_(); translated with t() where it is rendered. */
  title: string;
  icon?: ReactNode;
  render: (ctx: ResultViewContext) => ReactNode;
}
const resultViews: ResultView[] = [];
export function registerResultView(v: ResultView) {
  resultViews.push(v);
}
export function getResultViews() {
  return resultViews;
}

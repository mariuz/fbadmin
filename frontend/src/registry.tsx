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
      out.push(...fn(app, sel));
    } catch (e) {
      console.error("menu contributor failed", e);
    }
  }
  return out;
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

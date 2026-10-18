/** Loads frontend plugins listed by GET /api/plugins and hands them the fbadmin SDK. */
import * as React from "react";
import { api, request } from "./api";
import * as registry from "./registry";
import { registerCatalog, t } from "./i18n";
import { getPrefs } from "./prefs";

export const SDK = {
  version: 1,
  React,
  api,
  request,
  t,
  registerCatalog,
  getPrefs,
  registry: {
    registerTabKind: registry.registerTabKind,
    registerMenu: registry.registerMenu,
    registerTopbar: registry.registerTopbar,
    registerObjectTab: registry.registerObjectTab,
    registerDashboardSection: registry.registerDashboardSection,
    registerTreeNodes: registry.registerTreeNodes,
  },
};

declare global {
  interface Window { fbadmin?: typeof SDK }
}

let loading: Promise<void> | null = null;

/** Load plugins once per page (safe to call repeatedly). */
export function loadPlugins(): Promise<void> {
  loading ??= doLoadPlugins();
  return loading;
}

async function doLoadPlugins(): Promise<void> {
  window.fbadmin = SDK;
  let list: Array<{ name: string; frontend: string | null; error?: string }> = [];
  try {
    const r = await fetch("/api/plugins");
    if (r.ok) list = await r.json();
  } catch { /* backend unreachable: start without plugins */ }
  for (const p of list) {
    if (!p.frontend) continue;
    try {
      const mod = await import(/* @vite-ignore */ p.frontend);
      const init = mod.default ?? mod.setup;
      if (typeof init === "function") await init(SDK);
    } catch (e) {
      console.error(`fbadmin plugin ${p.name} failed`, e);
    }
  }
}

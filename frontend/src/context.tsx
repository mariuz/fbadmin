import { createContext, useContext, type ReactNode } from "react";
import type { Selection, Server } from "./types";

/** Built-in kinds plus any kind registered by a feature (see registry.tsx). */
export type WorkTabKind = string;

export interface WorkTab {
  id: string;
  kind: WorkTabKind;
  title: string;
  serverId?: string;
  dbId?: string;
  name?: string;
  otype?: string;
  initialSql?: string;
  autorun?: boolean;
  /** Feature-specific data for registered tab kinds. */
  props?: Record<string, any>;
}

export interface AppApi {
  servers: Server[];
  reloadServers: () => Promise<Server[]>;
  selection: Selection | null;
  select: (s: Selection | null) => void;
  openTab: (t: Omit<WorkTab, "id"> & { id?: string }) => void;
  closeTab: (id: string) => void;
  showDialog: (d: ReactNode | null) => void;
  toast: (msg: string, kind?: "ok" | "error" | "info") => void;
  invalidate: (key: string) => void;
  versions: Record<string, number>;
  openQueryTool: (serverId: string, dbId: string, sql?: string, autorun?: boolean) => void;
  jobStarted: () => void;
}

export const AppCtx = createContext<AppApi>(null as unknown as AppApi);
export const useApp = () => useContext(AppCtx);

export const keys = {
  server: (sid: string) => `s:${sid}`,
  db: (sid: string, dbid: string) => `d:${sid}:${dbid}`,
  folder: (sid: string, dbid: string, otype: string) => `f:${sid}:${dbid}:${otype}`,
  object: (sid: string, dbid: string, otype: string, name: string) => `o:${sid}:${dbid}:${otype}:${name}`,
};

export function selectionKey(s: Selection | null): string {
  if (!s) return "";
  switch (s.kind) {
    case "server": return keys.server(s.serverId);
    case "database": return keys.db(s.serverId, s.dbId!);
    case "folder": return keys.folder(s.serverId, s.dbId!, s.otype!);
    case "object": return keys.object(s.serverId, s.dbId!, s.otype!, s.name!);
    case "column": return keys.object(s.serverId, s.dbId!, s.otype!, s.name!) + ":" + s.column;
  }
}

export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

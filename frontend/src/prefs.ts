/** User preferences stored in the browser (per user, per device). */
import { useEffect, useState } from "react";

export interface Prefs {
  theme: "" | "light" | "dark" | "contrast";
  language: string;
  editorFontSize: number;
  nullDisplay: string;
  dateFormat: "iso" | "locale";
  confirmDrop: boolean;
  minimap: boolean;
  /** action id -> CodeMirror key spec, e.g. { execute: "Mod-Enter" } */
  keys: Record<string, string>;
  /** Ctrl+1..9 macros: SQL run in the Query Tool */
  macros: Array<{ key: string; name: string; sql: string }>;
}

export const DEFAULT_PREFS: Prefs = {
  theme: "",
  language: "",
  editorFontSize: 13,
  nullDisplay: "null",
  dateFormat: "iso",
  confirmDrop: true,
  minimap: false,
  keys: {},
  macros: [],
};

const KEY = "fbadmin.prefs";
const listeners = new Set<(p: Prefs) => void>();

export function getPrefs(): Prefs {
  try {
    return { ...DEFAULT_PREFS, ...(JSON.parse(localStorage.getItem(KEY) || "{}") as Partial<Prefs>) };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function setPrefs(update: Partial<Prefs>) {
  const next = { ...getPrefs(), ...update };
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch { /* storage unavailable */ }
  listeners.forEach((l) => l(next));
}

export function usePrefs(): Prefs {
  const [p, setP] = useState(getPrefs);
  useEffect(() => {
    listeners.add(setP);
    return () => { listeners.delete(setP); };
  }, []);
  return p;
}

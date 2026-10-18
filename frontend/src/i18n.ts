/**
 * Minimal i18n: t("English text") returns the translation for the current language.
 * Catalogs live in src/locales/<code>.json (keys are the English strings).
 */
import { getPrefs } from "./prefs";

export const LANGUAGES: Array<[string, string]> = [["en", "English"]];
const catalogs: Record<string, Record<string, string>> = {};

export function registerCatalog(code: string, name: string, messages: Record<string, string>) {
  catalogs[code] = messages;
  if (!LANGUAGES.some(([c]) => c === code)) LANGUAGES.push([code, name]);
}

export function currentLanguage(): string {
  const pref = getPrefs().language;
  if (pref) return pref;
  const nav = (typeof navigator !== "undefined" ? navigator.language : "en") || "en";
  if (catalogs[nav]) return nav;
  const base = nav.split("-")[0];
  return catalogs[base] ? base : "en";
}

/** Translate, then substitute {name} placeholders. */
export function t(text: string, vars?: Record<string, string | number>): string {
  const cat = catalogs[currentLanguage()];
  let out = (cat && cat[text]) || text;
  if (vars) for (const [k, v] of Object.entries(vars)) out = out.split(`{${k}}`).join(String(v));
  return out;
}

/** Marks a string for extraction without translating it (for module-level constants);
 *  translate it where it is shown with t(value). */
export function N_(text: string): string {
  return text;
}

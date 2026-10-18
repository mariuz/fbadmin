import { useState } from "react";
import { useApp } from "../context";
import { DEFAULT_PREFS, getPrefs, setPrefs, type Prefs } from "../prefs";
import { EDITOR_ACTIONS } from "./SqlEditor";
import { Modal } from "./Modal";
import { IconPlus, IconTrash } from "./Icons";
import { LANGUAGES, t } from "../i18n";

/** Preferences: appearance, editor, key bindings and Query Tool macros (stored per browser). */
export function PreferencesDialog() {
  const app = useApp();
  const [p, setP] = useState<Prefs>(getPrefs);
  const [tab, setTab] = useState<"general" | "editor" | "keys" | "macros">("general");
  const set = <K extends keyof Prefs>(k: K, v: Prefs[K]) => setP((x) => ({ ...x, [k]: v }));
  const close = () => app.showDialog(null);
  const save = () => {
    setPrefs(p);
    app.toast(t("Preferences saved"));
    close();
  };
  return (
    <Modal
      wide
      title={t("Preferences")}
      onClose={close}
      footer={<>
        <button className="btn" onClick={() => setP({ ...DEFAULT_PREFS })} style={{ marginRight: "auto" }}>{t("Reset to defaults")}</button>
        <button className="btn" onClick={close}>{t("Cancel")}</button>
        <button className="btn primary" onClick={save}>{t("Save")}</button>
      </>}
    >
      <div className="subtabs" role="tablist">
        {(["general", "editor", "keys", "macros"] as const).map((k) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
            {{ general: t("General"), editor: t("Editor"), keys: t("Keyboard"), macros: t("Macros") }[k]}
          </button>
        ))}
      </div>
      {tab === "general" && (
        <div className="form">
          <label className="l" htmlFor="pref-theme">{t("Theme")}</label>
          <select id="pref-theme" className="text" value={p.theme} onChange={(e) => set("theme", e.target.value as Prefs["theme"])}>
            <option value="">{t("Automatic (system)")}</option>
            <option value="light">{t("Light")}</option>
            <option value="dark">{t("Dark")}</option>
            <option value="contrast">{t("High contrast")}</option>
          </select>
          <label className="l" htmlFor="pref-lang">{t("Language")}</label>
          <select id="pref-lang" className="text" value={p.language} onChange={(e) => set("language", e.target.value)}>
            <option value="">{t("Browser default")}</option>
            {LANGUAGES.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
          </select>
          <label className="l" htmlFor="pref-null">{t("NULL display")}</label>
          <input id="pref-null" className="text" value={p.nullDisplay} onChange={(e) => set("nullDisplay", e.target.value)} />
          <label className="l" htmlFor="pref-date">{t("Dates and times")}</label>
          <select id="pref-date" className="text" value={p.dateFormat} onChange={(e) => set("dateFormat", e.target.value as Prefs["dateFormat"])}>
            <option value="iso">ISO 8601 (2026-10-18 17:49:22)</option>
            <option value="locale">{t("Browser locale")}</option>
          </select>
          <span />
          <label className="check"><input type="checkbox" checked={p.confirmDrop} onChange={(e) => set("confirmDrop", e.target.checked)} /> {t("Ask before dropping objects")}</label>
        </div>
      )}
      {tab === "editor" && (
        <div className="form">
          <label className="l" htmlFor="pref-font">{t("Font size")}</label>
          <input id="pref-font" className="text" type="number" min={9} max={28} value={p.editorFontSize} onChange={(e) => set("editorFontSize", Number(e.target.value) || 13)} />
          <span />
          <label className="check"><input type="checkbox" checked={p.minimap} onChange={(e) => set("minimap", e.target.checked)} /> {t("Show minimap")}</label>
        </div>
      )}
      {tab === "keys" && (
        <>
          <p className="muted" style={{ marginTop: 0 }}>
            {t("Use CodeMirror key names, comma separated: Mod-Enter, F5, Shift-Alt-f, Ctrl-k. Mod is Ctrl on Windows/Linux and Cmd on macOS. Changes apply to newly opened Query Tool tabs.")}
          </p>
          <div className="form">
            {EDITOR_ACTIONS.map((a) => (
              <div key={a.id} style={{ display: "contents" }}>
                <label className="l" htmlFor={"key-" + a.id}>{t(a.label)}</label>
                <input id={"key-" + a.id} className="text mono" placeholder={a.keys.join(", ")} value={p.keys[a.id] ?? ""}
                  onChange={(e) => set("keys", { ...p.keys, [a.id]: e.target.value })} />
              </div>
            ))}
          </div>
        </>
      )}
      {tab === "macros" && (
        <>
          <p className="muted" style={{ marginTop: 0 }}>
            {t("Macros run in the Query Tool with Ctrl/Cmd+1…9. $SELECTION$ is replaced by the selected text.")}
          </p>
          <table className="coltable" style={{ width: "100%" }}>
            <thead><tr className="muted"><td style={{ width: 70 }}>{t("Key")}</td><td style={{ width: 200 }}>{t("Name")}</td><td>SQL</td><td /></tr></thead>
            <tbody>
              {p.macros.map((m, i) => (
                <tr key={i}>
                  <td>
                    <select className="text" value={m.key} onChange={(e) => set("macros", p.macros.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))}>
                      {["1", "2", "3", "4", "5", "6", "7", "8", "9"].map((k) => <option key={k} value={k}>Mod-{k}</option>)}
                    </select>
                  </td>
                  <td><input className="text" value={m.name} onChange={(e) => set("macros", p.macros.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} /></td>
                  <td><textarea className="text mono" rows={2} style={{ width: "100%" }} value={m.sql} onChange={(e) => set("macros", p.macros.map((x, j) => (j === i ? { ...x, sql: e.target.value } : x)))} /></td>
                  <td><button className="btn icon small" aria-label={t("Remove macro")} onClick={() => set("macros", p.macros.filter((_, j) => j !== i))}><IconTrash size={13} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="btn small" style={{ marginTop: 8 }} onClick={() => {
            const used = new Set(p.macros.map((m) => m.key));
            const key = ["1", "2", "3", "4", "5", "6", "7", "8", "9"].find((k) => !used.has(k)) ?? "1";
            set("macros", [...p.macros, { key, name: "Count rows", sql: "SELECT COUNT(*) FROM $SELECTION$;" }]);
          }}><IconPlus size={13} /> {t("Add macro")}</button>
        </>
      )}
    </Modal>
  );
}

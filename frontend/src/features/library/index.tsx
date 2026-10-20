/**
 * Milestone 2.0 — query library: saved queries and snippets kept on the fbadmin server, private or
 * shared with every fbadmin user in server mode, with folders, tags, search and revision history.
 * "Save to library" in the Query Tool toolbar saves the selection (or the whole editor).
 */
import { useEffect, useState } from "react";
import "./library.css";
import { registerQueryToolAction, registerTabKind, registerTopbar, type QueryToolContext } from "../../registry";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { Modal } from "../../components/Modal";
import { IconSave } from "../../components/Icons";
import { lapi, migrateBrowserSnippets, type LibraryInfo } from "./api";
import { ItemFields, LibraryPanel, fromForm, toForm, type ItemForm } from "./Library";

function SaveDialog(props: { ctx: QueryToolContext; sql: string; onClose: () => void }) {
  const app = useApp();
  const [info, setInfo] = useState<LibraryInfo | null>(null);
  const firstLine = props.sql.trim().split("\n")[0].replace(/^--\s*/, "").slice(0, 60);
  const [form, setForm] = useState<ItemForm>(() => toForm({
    name: firstLine, sql: props.sql, server_id: props.ctx.serverId, database_id: props.ctx.dbId,
  }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    lapi.info().then((i) => { setInfo(i); migrateBrowserSnippets(i.user).catch(() => 0); }).catch((e) => setError(errMsg(e)));
  }, []);
  const save = async () => {
    setBusy(true); setError(null);
    try {
      await lapi.create(fromForm(form));
      app.toast(t("Saved to the library"));
      props.onClose();
    } catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  };
  return (
    <Modal title={t("Save to library")} onClose={props.onClose} wide footer={<>
      <button className="btn" onClick={() => { props.onClose(); app.openTab({ kind: "library", title: t("Library") }); }}>{t("Open the library")}</button>
      <span style={{ flex: 1 }} />
      <button className="btn" onClick={props.onClose}>{t("Cancel")}</button>
      <button className="btn primary" disabled={busy || !form.name.trim()} onClick={save}>{t("Save")}</button>
    </>}>
      <ItemFields form={form} set={setForm} info={info} idPrefix="lib-save" canShare />
      <div className="lib-sql-label muted">{t("SQL")}</div>
      <pre className="lib-preview mono" role="region" aria-label={t("SQL to save")} tabIndex={0}>{form.sql}</pre>
      {error && <div className="err" role="alert">{error}</div>}
    </Modal>
  );
}

registerTabKind("library", {
  icon: <IconSave size={13} />,
  render: (_tab, visible) => <LibraryPanel visible={visible} />,
  same: () => true,
});

registerTopbar({
  id: "library",
  render: (app) => <button className="tb" onClick={() => app.openTab({ kind: "library", title: t("Library") })}>📚 {t("Library")}</button>,
});

registerQueryToolAction({
  id: "library",
  render: (ctx) => <SaveToLibraryButton ctx={ctx} />,
});

function SaveToLibraryButton(props: { ctx: QueryToolContext }) {
  const app = useApp();
  return (
    <button className="btn icon" title={t("Save to library (the selection, or the whole editor)")} aria-label={t("Save to library")}
      onClick={() => {
        const sql = props.ctx.selectionOrAll();
        if (!sql.trim()) { app.toast(t("Nothing to save: the editor is empty."), "error"); return; }
        app.showDialog(<SaveDialog ctx={props.ctx} sql={sql} onClose={() => app.showDialog(null)} />);
      }}><IconSave /></button>
  );
}

/** Roadmap 2.3: SQL notebooks - SQL cells, notes, results and charts in one saved document, shared in server mode. */
import { useEffect, useRef, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { t } from "../../i18n";
import { registerMenu, registerTabKind, registerTopbar } from "../../registry";
import { fmtTime } from "../maintenance/api";
import { nbApi, type NotebookMeta } from "./api";
import { NotebookTab, openNotebook } from "./Notebook";
import "./notebooks.css";

let whoami: Promise<{ user: string | null; serverMode: boolean }> | null = null;
const who = () => (whoami ??= nbApi.list({ q: "\u0000" }).then((r) => ({ user: r.user, serverMode: r.server_mode })).catch(() => ({ user: null, serverMode: false })));

function useWho() {
  const [w, setW] = useState<{ user: string | null; serverMode: boolean } | null>(null);
  useEffect(() => { who().then(setW); }, []);
  return w;
}

export async function newNotebook(app: AppApi, sid?: string, dbid?: string) {
  try {
    const nb = await nbApi.create({ name: t("Untitled notebook"), server_id: sid ?? null, database_id: dbid ?? null });
    openNotebook(app, nb.id, nb.name);
  } catch (e) { app.toast(errMsg(e), "error"); }
}

function NotebookList(props: { visible: boolean }) {
  const app = useApp();
  const w = useWho();
  const [q, setQ] = useState("");
  const [scope, setScope] = useState("all");
  const [items, setItems] = useState<NotebookMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const load = () => nbApi.list({ q: q || undefined, scope }).then((r) => { setItems(r.notebooks); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { if (props.visible) load(); }, [props.visible, scope]);
  useEffect(() => { const h = window.setTimeout(load, 250); return () => window.clearTimeout(h); }, [q]);
  const dbName = (n: NotebookMeta) => {
    const s = app.servers.find((x) => x.id === n.server_id);
    const d = s?.databases.find((x) => x.id === n.database_id);
    return s && d ? `${s.name} · ${d.name}` : n.database_id ? t("(not available to you)") : "—";
  };
  return (
    <div className="nb-list">
      <div className="nb-head">
        <h3>{t("Notebooks")}</h3>
        <input className="text" type="search" aria-label={t("Search notebooks")} placeholder={t("Search names, notes and SQL…")} value={q} onChange={(e) => setQ(e.target.value)} />
        {w?.serverMode && <div className="seg" role="group" aria-label={t("Show")}>
          {[["all", t("All")], ["mine", t("Mine")], ["shared", t("Shared")]].map(([k, l]) => (
            <button key={k} className={scope === k ? "on" : ""} aria-pressed={scope === k} onClick={() => setScope(k)}>{l}</button>
          ))}
        </div>}
        <span style={{ flex: 1 }} />
        <button className="btn small primary" onClick={() => newNotebook(app, app.selection?.serverId, app.selection?.dbId ?? undefined)}>{t("New notebook")}</button>
        <button className="btn small" onClick={() => file.current?.click()}>{t("Import…")}</button>
        <input ref={file} type="file" accept=".json,application/json" hidden aria-hidden="true" onChange={async (e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (!f) return;
          try {
            const nb = await nbApi.importDoc(JSON.parse(await f.text()), app.selection?.serverId ?? null, app.selection?.dbId ?? null);
            openNotebook(app, nb.id, nb.name); load();
          } catch (err) { app.toast(errMsg(err), "error"); }
        }} />
      </div>
      <p className="muted">{t("SQL cells, notes, results and charts in one saved document: runbooks such as a monthly health check, or an investigation to hand over.")}
        {w?.serverMode ? " " + t("Shared notebooks are visible to every fbadmin user; their results only to users who may read the database.") : ""}</p>
      {error && <div className="err">{error}</div>}
      {items && items.length === 0 && <div className="muted">{q ? t("No notebook matches.") : t("No notebooks yet.")}</div>}
      {items && items.length > 0 && <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Notebooks")}><table className="grid">
        <thead><tr><th>{t("Name")}</th><th>{t("Database")}</th>{w?.serverMode && <><th>{t("Owner")}</th><th>{t("Visibility")}</th></>}<th>{t("Cells")}</th><th>{t("Updated")}</th></tr></thead>
        <tbody>{items.map((n) => (
          <tr key={n.id} tabIndex={0} style={{ cursor: "pointer" }} onClick={() => openNotebook(app, n.id, n.name)}
            onKeyDown={(e) => e.key === "Enter" && openNotebook(app, n.id, n.name)}>
            <td><strong>{n.name}</strong>{n.description && <div className="muted nb-sub">{n.description}</div>}</td>
            <td>{dbName(n)}</td>
            {w?.serverMode && <><td>{n.owner ?? "—"}</td><td>{n.visibility === "shared" ? t("Shared") : t("Private")}</td></>}
            <td className="num">{n.cell_count}</td>
            <td>{fmtTime(n.updated_at)}{n.updated_by ? ` · ${n.updated_by}` : ""}</td>
          </tr>
        ))}</tbody>
      </table></div>}
    </div>
  );
}

function NotebookHost(props: { id: string; visible: boolean }) {
  const w = useWho();
  if (!w) return null;
  return <NotebookTab id={props.id} visible={props.visible} user={w.user} serverMode={w.serverMode} />;
}

registerTabKind("notebooks", {
  icon: <span aria-hidden="true">📓</span>,
  render: (_tab, visible) => <NotebookList visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});
registerTabKind("notebook", {
  icon: <span aria-hidden="true">📓</span>,
  render: (tab, visible) => <NotebookHost id={tab.props!.id} visible={visible} />,
  same: (a, b) => a.kind === b.kind && a.props?.id === b.props?.id,
});

registerTopbar({
  id: "notebooks",
  render: (app) => <button className="tb" onClick={() => app.openTab({ kind: "notebooks", title: t("Notebooks") })}>📓 {t("Notebooks")}</button>,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("New notebook…"), onClick: () => newNotebook(app, sid, dbid) }];
});

import { useEffect, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp, type AppApi } from "../../context";
import { t } from "../../i18n";
import { SqlEditor } from "../../components/SqlEditor";
import { IconCopy, IconFolder, IconPlay, IconPlus, IconRefresh, IconTerminal, IconTrash } from "../../components/Icons";
import { fmtTime } from "../maintenance/api";
import { lapi, migrateBrowserSnippets, type LibraryInfo, type LibraryItem, type LibraryRevision, type Visibility } from "./api";

// ------------------------------------------------------------------ helpers
/** Server/database the item opens on: its default when registered here, else the selected database. */
export function targetDb(app: AppApi, it: { server_id: string | null; database_id: string | null }): { sid: string; dbid: string } | null {
  if (it.server_id && it.database_id) {
    const srv = app.servers.find((s) => s.id === it.server_id);
    if (srv?.databases.some((d) => d.id === it.database_id)) return { sid: it.server_id, dbid: it.database_id };
  }
  const sel = app.selection;
  if (sel?.dbId) return { sid: sel.serverId, dbid: sel.dbId };
  return null;
}

export function dbLabel(app: AppApi, sid: string | null, dbid: string | null): string | null {
  if (!sid || !dbid) return null;
  const srv = app.servers.find((s) => s.id === sid);
  const db = srv?.databases.find((d) => d.id === dbid);
  return srv && db ? `${srv.name} / ${db.name}` : null;
}

export function openItem(app: AppApi, it: LibraryItem, run: boolean) {
  const target = targetDb(app, it);
  if (!target) {
    app.toast(t("Select a database in the Object Explorer first: this query has no default database here."), "error");
    return;
  }
  app.openQueryTool(target.sid, target.dbid, it.sql, run);
}

const splitTags = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export interface ItemForm {
  name: string; description: string; folder: string; tags: string; visibility: Visibility; target: string; sql: string;
}
export const toForm = (it: Partial<LibraryItem>): ItemForm => ({
  name: it.name ?? "", description: it.description ?? "", folder: it.folder ?? "", tags: (it.tags ?? []).join(", "),
  visibility: it.visibility ?? "private", target: it.server_id && it.database_id ? `${it.server_id}/${it.database_id}` : "",
  sql: it.sql ?? "",
});
export const fromForm = (f: ItemForm) => {
  const [server_id, database_id] = f.target ? f.target.split("/") : [null, null];
  return { name: f.name, description: f.description, folder: f.folder, tags: splitTags(f.tags), visibility: f.visibility,
    server_id, database_id, sql: f.sql };
};

/** Name, description, folder, tags, visibility and default database fields (shared with the save dialog). */
export function ItemFields(props: { form: ItemForm; set: (f: ItemForm) => void; info: LibraryInfo | null; idPrefix: string; canShare: boolean }) {
  const app = useApp();
  const { form, set, info, idPrefix: p } = props;
  return (
    <div className="form mform">
      <label className="l" htmlFor={`${p}-name`}>{t("Name")}</label>
      <input id={`${p}-name`} className="text" value={form.name} autoFocus onChange={(e) => set({ ...form, name: e.target.value })} />
      <label className="l" htmlFor={`${p}-desc`}>{t("Description")}</label>
      <textarea id={`${p}-desc`} className="text" rows={2} value={form.description} onChange={(e) => set({ ...form, description: e.target.value })} />
      <label className="l" htmlFor={`${p}-folder`}>{t("Folder")}</label>
      <input id={`${p}-folder`} className="text" list={`${p}-folders`} placeholder={t("e.g. Reports/Monthly")} value={form.folder}
        onChange={(e) => set({ ...form, folder: e.target.value })} />
      <datalist id={`${p}-folders`}>{info?.folders.map((f) => <option key={f.path} value={f.path} />)}</datalist>
      <label className="l" htmlFor={`${p}-tags`}>{t("Tags (comma-separated)")}</label>
      <input id={`${p}-tags`} className="text" value={form.tags} onChange={(e) => set({ ...form, tags: e.target.value })} />
      {info?.server_mode && <>
        <label className="l" htmlFor={`${p}-vis`}>{t("Visibility")}</label>
        <select id={`${p}-vis`} className="text" value={form.visibility} disabled={!props.canShare}
          onChange={(e) => set({ ...form, visibility: e.target.value as Visibility })}>
          <option value="private">{t("Private (only me)")}</option>
          <option value="shared">{t("Shared with every fbadmin user")}</option>
        </select>
      </>}
      <label className="l" htmlFor={`${p}-db`}>{t("Default database")}</label>
      <select id={`${p}-db`} className="text" value={form.target} onChange={(e) => set({ ...form, target: e.target.value })}>
        <option value="">{t("None (use the selected database)")}</option>
        {form.target && !dbLabel(app, ...(form.target.split("/") as [string, string])) && <option value={form.target}>{t("(not registered here)")}</option>}
        {app.servers.filter((s) => s.databases.length).map((s) => (
          <optgroup key={s.id} label={s.name}>
            {s.databases.map((d) => <option key={d.id} value={`${s.id}/${d.id}`}>{`${s.name} / ${d.name}`}</option>)}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

function Params(props: { sql: string }) {
  const [names, setNames] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    api.queryParams(props.sql).then((r) => live && setNames(r.params)).catch(() => live && setNames([]));
    return () => { live = false; };
  }, [props.sql]);
  if (!names.length) return null;
  return <div className="lib-meta"><span className="muted">{t("Parameters (asked for when run)")}</span> {names.map((n) => <code key={n} className="lib-param">:{n}</code>)}</div>;
}

// ------------------------------------------------------------------ panel
export function LibraryPanel(props: { visible: boolean }) {
  const app = useApp();
  const [info, setInfo] = useState<LibraryInfo | null>(null);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [q, setQ] = useState("");
  const [folder, setFolder] = useState<string | null>(null);
  const [tag, setTag] = useState<string | null>(null);
  const [scope, setScope] = useState<"all" | "mine" | "shared">("all");
  const [selId, setSelId] = useState<string | null>(null);
  const [detail, setDetail] = useState<LibraryItem | null>(null);
  const [form, setForm] = useState<(ItemForm & { id?: string }) | null>(null);
  const [history, setHistory] = useState(false);
  const [viewRev, setViewRev] = useState<LibraryRevision | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const i = await lapi.info();
      setInfo(i);
      const n = await migrateBrowserSnippets(i.user).catch(() => 0);
      if (n) {
        app.toast(t("Imported {count} snippet(s) from this browser into the library.", { count: n }), "info");
        setInfo(await lapi.info());
      }
      setItems(await lapi.items({ q, folder: folder ?? undefined, tag: tag ?? undefined, scope }));
    } catch (e) { setError(errMsg(e)); }
  };
  useEffect(() => {
    if (!props.visible) return;
    const timer = setTimeout(load, q ? 250 : 0);
    return () => clearTimeout(timer);
  }, [props.visible, q, folder, tag, scope]);

  useEffect(() => {
    setHistory(false);
    setViewRev(null);
    if (!selId) { setDetail(null); return; }
    lapi.item(selId).then(setDetail).catch((e) => { setDetail(null); setError(errMsg(e)); });
  }, [selId]);

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true); setError(null);
    try { await fn(); if (ok) app.toast(ok); } catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  };
  const reloadAll = async (keep?: string | null) => {
    await load();
    const id = keep === undefined ? selId : keep;
    setSelId(id ?? null);
    if (id) setDetail(await lapi.item(id));
  };

  const save = () => act(async () => {
    if (!form) return;
    const body = fromForm(form);
    const it = form.id ? await lapi.update(form.id, body) : await lapi.create(body);
    setForm(null);
    await reloadAll(it.id);
  }, t("Saved"));

  const folders = info?.folders ?? [];
  const tags = info?.tags ?? [];
  const sel = detail && detail.id === selId ? detail : null;
  const target = sel ? targetDb(app, sel) : null;
  const shownSql = viewRev?.sql ?? sel?.sql ?? "";

  return (
    <div className="lib">
      <section className="lib-side" aria-label={t("Library filters")}>
        <input className="text" type="search" aria-label={t("Search the library")} placeholder={t("Search name, tags, SQL…")}
          value={q} onChange={(e) => setQ(e.target.value)} />
        {info?.server_mode && (
          <select className="text" aria-label={t("Show")} value={scope} onChange={(e) => setScope(e.target.value as typeof scope)}>
            <option value="all">{t("All items I can see")}</option>
            <option value="mine">{t("My items")}</option>
            <option value="shared">{t("Shared items")}</option>
          </select>
        )}
        <h4>{t("Folders")}</h4>
        <ul className="lib-nav">
          <li><button className={folder === null ? "active" : ""} aria-pressed={folder === null} onClick={() => setFolder(null)}>{t("All folders")}</button></li>
          {folders.map((f) => (
            <li key={f.path}>
              <button className={folder === f.path ? "active" : ""} aria-pressed={folder === f.path} title={f.path}
                style={{ paddingLeft: 8 + 12 * (f.path.split("/").length - 1) }} onClick={() => setFolder(folder === f.path ? null : f.path)}>
                <IconFolder size={13} /> {f.path.split("/").pop()} <span className="muted">{f.count}</span>
              </button>
            </li>
          ))}
        </ul>
        {tags.length > 0 && <>
          <h4>{t("Tags")}</h4>
          <div className="lib-tags">
            {tags.map((tg) => (
              <button key={tg.tag} className={"lib-tag" + (tag === tg.tag ? " active" : "")} aria-pressed={tag === tg.tag}
                onClick={() => setTag(tag === tg.tag ? null : tg.tag)}>#{tg.tag} <span className="muted">{tg.count}</span></button>
            ))}
          </div>
        </>}
      </section>

      <section className="lib-list" aria-label={t("Saved queries")}>
        <div className="toolbar">
          <span className="muted">{t("{count} item(s)", { count: items.length })}</span>
          <span className="grow" />
          <button className="btn small" onClick={() => reloadAll()} aria-label={t("Refresh")} title={t("Refresh")}><IconRefresh size={12} /></button>
          <button className="btn small primary" onClick={() => { setSelId(null); setForm({ ...toForm({ folder: folder ?? "" }) }); }}>
            <IconPlus size={12} /> {t("New query")}
          </button>
        </div>
        {items.length === 0 ? (
          <div className="pad muted">{q || folder || tag ? t("Nothing matches.") : t("The library is empty. Save queries from the Query Tool with “Save to library”, or create one here.")}</div>
        ) : (
          <ul className="lib-items">
            {items.map((it) => (
              <li key={it.id}>
                <button className={"lib-item" + (it.id === selId ? " active" : "")} aria-current={it.id === selId ? "true" : undefined}
                  onClick={() => { setForm(null); setSelId(it.id); }}
                  onDoubleClick={() => openItem(app, it, false)}>
                  <span className="lib-item-name">{it.name}</span>
                  <span className="lib-item-sub">
                    {it.folder && <span>{it.folder}</span>}
                    {it.tags.map((tg) => <span key={tg} className="lib-chip">#{tg}</span>)}
                    {info?.server_mode && <span className={"pill " + (it.visibility === "shared" ? "lib-shared" : "")}>{it.visibility === "shared" ? t("shared") : t("private")}</span>}
                    {info?.server_mode && !it.mine && it.owner && <span className="muted">{t("by {user}", { user: it.owner })}</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="lib-detail" aria-label={t("Query details")}>
        {error && <div className="err" role="alert" style={{ marginBottom: 8 }}>{error}</div>}
        {form ? (
          <div>
            <h3>{form.id ? t("Edit query") : t("New query")}</h3>
            <ItemFields form={form} set={(f) => setForm({ ...f, id: form.id })} info={info} idPrefix="lib-f"
              canShare={!form.id || !!sel?.mine || !info?.server_mode} />
            <div className="lib-sql-label muted">{t("SQL")}</div>
            <div className="lib-sql"><SqlEditor value={form.sql} onChange={(v) => setForm((f) => f && { ...f, sql: v })} /></div>
            <div className="mt-row" style={{ marginTop: 10 }}>
              <button className="btn primary" disabled={busy || !form.name.trim() || !form.sql.trim()} onClick={save}>{t("Save")}</button>
              <button className="btn" onClick={() => setForm(null)}>{t("Cancel")}</button>
            </div>
          </div>
        ) : !sel ? (
          <div className="empty-state"><div className="big">{t("Query library")}</div>
            <div>{info?.server_mode
              ? t("Saved queries and snippets, private or shared with your team. Pick one on the left.")
              : t("Saved queries and snippets. Pick one on the left.")}</div></div>
        ) : (
          <div>
            <h3>{sel.name}</h3>
            <div className="lib-meta">
              {info?.server_mode && <span className={"pill " + (sel.visibility === "shared" ? "lib-shared" : "")}>{sel.visibility === "shared" ? t("shared") : t("private")}</span>}
              {info?.server_mode && sel.owner && <span>{t("Owner: {user}", { user: sel.owner })}</span>}
              <span className="muted">{t("Revision {rev} · {time}", { rev: sel.rev, time: fmtTime(sel.updated_at) })}{sel.updated_by ? ` · ${sel.updated_by}` : ""}</span>
            </div>
            {sel.description && <p className="lib-desc">{sel.description}</p>}
            <div className="lib-meta">
              {sel.folder && <span><IconFolder size={12} /> {sel.folder}</span>}
              {sel.tags.map((tg) => <span key={tg} className="lib-chip">#{tg}</span>)}
            </div>
            <div className="lib-meta">
              <span className="muted">{t("Opens on")}</span>{" "}
              {target ? <b>{dbLabel(app, target.sid, target.dbid)}</b> : <span className="warn">{t("no database: select one in the Object Explorer")}</span>}
              {sel.server_id && !dbLabel(app, sel.server_id, sel.database_id) && <span className="muted">{t("(its default database is not registered here)")}</span>}
            </div>
            <Params sql={sel.sql} />
            <div className="mt-row" style={{ margin: "10px 0" }}>
              <button className="btn primary" disabled={!target} onClick={() => openItem(app, sel, true)}><IconPlay size={12} /> {t("Run")}</button>
              <button className="btn" disabled={!target} onClick={() => openItem(app, sel, false)}><IconTerminal size={12} /> {t("Open in Query Tool")}</button>
              {sel.can_edit && <button className="btn" onClick={() => setForm({ ...toForm(sel), id: sel.id })}>{t("Edit")}</button>}
              <button className="btn" disabled={busy} onClick={() => act(async () => {
                const name = window.prompt(t("Name of the copy"), t("{name} (copy)", { name: sel.name }));
                if (!name) return;
                const cp = await lapi.copy(sel.id, name);
                await reloadAll(cp.id);
              }, t("Copy saved"))}><IconCopy size={12} /> {t("Save a copy")}</button>
              <button className={"btn" + (history ? " on" : "")} aria-pressed={history} onClick={() => { setHistory(!history); setViewRev(null); }}>
                {t("History ({count})", { count: sel.revision_count })}
              </button>
              {sel.can_edit && <button className="btn danger" disabled={busy} onClick={() => act(async () => {
                if (!confirm(t("Delete “{name}” from the library?", { name: sel.name }))) return;
                await lapi.remove(sel.id);
                await reloadAll(null);
              }, t("Deleted"))}><IconTrash size={12} /> {t("Delete")}</button>}
            </div>
            {!sel.can_edit && info?.server_mode && <div className="hint muted">{t("Only the owner or an fbadmin administrator can change this shared query. Save a copy to change it.")}</div>}
            {history && sel.revisions && (
              <div className="grid-wrap" style={{ marginBottom: 10, maxHeight: 220 }}>
                <table className="grid">
                  <thead><tr><th>{t("Revision")}</th><th>{t("Saved")}</th><th>{t("By")}</th><th>{t("Name")}</th><th><span className="sr-only">{t("Actions")}</span></th></tr></thead>
                  <tbody>{sel.revisions.map((r) => (
                    <tr key={r.rev} className={viewRev?.rev === r.rev ? "sel" : ""}>
                      <td className="num">{r.rev}{r.rev === sel.rev ? ` (${t("current")})` : ""}</td>
                      <td>{fmtTime(r.at)}</td>
                      <td>{r.author ?? "—"}</td>
                      <td>{r.name}</td>
                      <td>
                        <button className="btn small" aria-pressed={viewRev?.rev === r.rev} onClick={() => setViewRev(viewRev?.rev === r.rev ? null : r)}>{t("View")}</button>{" "}
                        {sel.can_edit && r.rev !== sel.rev && <button className="btn small" disabled={busy} onClick={() => act(async () => {
                          await lapi.restore(sel.id, r.rev);
                          setViewRev(null);
                          await reloadAll(sel.id);
                        }, t("Revision {rev} restored", { rev: r.rev }))}>{t("Restore")}</button>}
                      </td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
            {viewRev && <div className="hint">{t("Showing revision {rev} of {time}", { rev: viewRev.rev, time: fmtTime(viewRev.at) })}</div>}
            <div className="lib-sql"><SqlEditor value={shownSql} readOnly /></div>
          </div>
        )}
      </section>
    </div>
  );
}

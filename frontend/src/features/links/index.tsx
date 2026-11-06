/** Roadmap 2.3: shareable links to objects, saved queries, notebooks and result snapshots, opened with the
 *  viewer's own permissions (the link grants nothing). Links look like <fbadmin>/#/link/<id>. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { N_, t } from "../../i18n";
import { registerAppEffect, registerMenu, registerResultView, registerTabKind, type ResultViewContext } from "../../registry";
import { Modal } from "../../components/Modal";
import { ResultGrid } from "../../components/ResultGrid";
import { IconCopy } from "../../components/Icons";
import { fmtTime } from "../maintenance/api";
import { openNotebook } from "../notebooks/Notebook";
import "./links.css";
import { fullUrl, linkApi, type LinkIn, type LinkInfo, type Resolved } from "./api";

const KIND: Record<string, string> = { object: N_("object"), query: N_("saved query"), notebook: N_("notebook"), snapshot: N_("result snapshot") };

/** Show a link with a copy button (the clipboard is not available on plain http outside localhost). */
function LinkDialog(props: { url: string; note?: string; onClose: () => void }) {
  const app = useApp();
  return (
    <Modal title={t("Link")} onClose={props.onClose}>
      <p className="muted">{props.note ?? t("Whoever opens it needs their own access in fbadmin: the link grants nothing.")}</p>
      <div className="mt-row">
        <input className="text lk-url" readOnly value={props.url} aria-label={t("Link")} onFocus={(e) => e.currentTarget.select()} autoFocus />
        <button className="btn small" onClick={async () => {
          try { await navigator.clipboard.writeText(props.url); app.toast(t("Link copied"), "ok"); } catch { app.toast(t("Select the link and copy it"), "info"); }
        }}><IconCopy size={12} /> {t("Copy")}</button>
      </div>
    </Modal>
  );
}

/** Make a link and copy it (or show it when copying is not allowed). */
export async function shareLink(app: AppApi, body: LinkIn): Promise<LinkInfo | null> {
  try {
    const lk = await linkApi.create(body);
    const url = fullUrl(lk.path);
    let copied = false;
    try { await navigator.clipboard.writeText(url); copied = true; } catch { /* insecure context */ }
    if (copied) app.toast(t("Link copied: {url}", { url }), "ok");
    else app.showDialog(<LinkDialog url={url} onClose={() => app.showDialog(null)} />);
    return lk;
  } catch (e) {
    app.toast(errMsg(e), "error");
    return null;
  }
}

function Denied(props: { reason: string; who?: string | null; onClose: () => void }) {
  return (
    <Modal title={t("This link cannot be opened")} onClose={props.onClose}>
      <p>{props.reason}</p>
      {props.who && <p className="muted">{t("Shared by {user}. Ask them, or an fbadmin administrator, for access.", { user: props.who })}</p>}
    </Modal>
  );
}

async function openLink(app: AppApi, id: string) {
  let r: Resolved;
  try {
    const res = await linkApi.open(id);
    if ("denied" in res) { app.showDialog(<Denied reason={res.denied} who={res.by} onClose={() => app.showDialog(null)} />); return; }
    if ("gone" in res) { app.showDialog(<Denied reason={res.gone} onClose={() => app.showDialog(null)} />); return; }
    r = res.ok;
  } catch (e) {
    app.showDialog(<Denied reason={errMsg(e)} onClose={() => app.showDialog(null)} />);
    return;
  }
  if (r.kind === "object" && r.server_id && r.database_id && r.otype && r.name) {
    app.select({ kind: "object", serverId: r.server_id, dbId: r.database_id, otype: r.otype, name: r.name, schema: r.schema ?? undefined });
    app.toast(t("Opened {name} from a link by {user}", { name: r.name, user: r.created_by ?? "?" }), "info");
  } else if (r.kind === "query") {
    if (r.server_id && r.database_id && app.servers.some((s) => s.id === r.server_id && s.databases.some((d) => d.id === r.database_id))) {
      app.openQueryTool(r.server_id, r.database_id, r.sql ?? "");
    } else {
      app.openTab({ kind: "library", title: t("Library") });
      app.toast(t("The saved query {name} has no database you can open: pick one in the Object Explorer", { name: r.name ?? "" }), "info");
    }
  } else if (r.kind === "notebook" && r.item_id) {
    openNotebook(app, r.item_id, r.name ?? t("Notebook"));
  } else if (r.kind === "snapshot") {
    app.openTab({ kind: "link-snapshot", title: r.title || t("Shared result"), props: { link: r } });
  }
}

const LINK_RE = /^#\/link\/([A-Za-z0-9_-]{6,64})$/;
const PENDING = "fbadmin.pendingLink";
// remembered before signing in: single sign-on redirects lose the address bar's #/link/... part
try {
  const m = LINK_RE.exec(location.hash);
  if (m) sessionStorage.setItem(PENDING, m[1]);
} catch { /* storage blocked */ }

/** Opens <fbadmin>/#/link/<id> when the main window is shown (after signing in) or the hash changes. */
registerAppEffect((getApp) => {
  let tm: number | undefined;
  const check = (tries = 0) => {
    let id = LINK_RE.exec(location.hash)?.[1] ?? null;
    if (!id) try { id = sessionStorage.getItem(PENDING); } catch { /* storage blocked */ }
    if (!id) return;
    if (!getApp().servers.length && tries < 20) { tm = window.setTimeout(() => check(tries + 1), 250); return; } // servers loading
    try { sessionStorage.removeItem(PENDING); } catch { /* storage blocked */ }
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    openLink(getApp(), id);
  };
  tm = window.setTimeout(() => check(), 100);
  const onHash = () => check();
  window.addEventListener("hashchange", onHash);
  return () => { window.clearTimeout(tm); window.removeEventListener("hashchange", onHash); };
});

function SnapshotTab(props: { link: Resolved }) {
  const app = useApp();
  const r = props.link;
  const s = r.snapshot;
  if (!s) return <div className="pad muted">{t("This snapshot is empty.")}</div>;
  const canOpen = !!(r.server_id && r.database_id && app.servers.some((x) => x.id === r.server_id && x.databases.some((d) => d.id === r.database_id)));
  return (
    <div className="lk-snap">
      <div className="lk-head">
        <h3>{r.title || t("Shared result")}</h3>
        <span className="muted">{t("{database} on {server} · shared by {user} · taken {time}", {
          database: r.database ?? "?", server: r.server ?? "?", user: r.created_by ?? "?", time: fmtTime(s.taken_at) })}</span>
        <span style={{ flex: 1 }} />
        {canOpen && s.sql && <button className="btn small" onClick={() => app.openQueryTool(r.server_id!, r.database_id!, s.sql)}>{t("Open the SQL in the Query Tool")}</button>}
      </div>
      <p className="muted">{s.truncated || (s.row_count ?? s.rows.length) > s.rows.length
        ? t("A copy of the first {n} rows, as they were when the link was made.", { n: s.rows.length })
        : t("A copy of the {n} row(s) as they were when the link was made.", { n: s.rows.length })}</p>
      {s.sql && <pre className="code-block lk-sql" tabIndex={0} aria-label={t("SQL")}>{s.sql}</pre>}
      <div className="lk-grid" role="region" aria-label={t("Shared rows")}><ResultGrid columns={s.columns} rows={s.rows} name="snapshot" /></div>
    </div>
  );
}

function ShareResult(props: { ctx: ResultViewContext }) {
  const app = useApp();
  const { ctx } = props;
  const [title, setTitle] = useState("");
  const [days, setDays] = useState("30");
  const [made, setMade] = useState<LinkInfo | null>(null);
  if (!ctx.serverId || !ctx.dbId) return <div className="pad muted">{t("Run the query in the Query Tool to share its result.")}</div>;
  return (
    <div className="lk-share pad">
      <p>{t("Share this result as a snapshot: a copy of the rows fetched so far ({n}), with the SQL. Only people who may read this database in fbadmin can open it.", { n: ctx.rows.length })}</p>
      <div className="form mform">
        <label className="l" htmlFor="lk-title">{t("Title")}</label>
        <input id="lk-title" className="text" value={title} placeholder={t("What this result shows")} onChange={(e) => setTitle(e.target.value)} />
        <label className="l" htmlFor="lk-days">{t("Expires")}</label>
        <select id="lk-days" className="text" value={days} onChange={(e) => setDays(e.target.value)}>
          <option value="1">{t("after 1 day")}</option>
          <option value="7">{t("after 7 days")}</option>
          <option value="30">{t("after 30 days")}</option>
          <option value="365">{t("after a year")}</option>
          <option value="">{t("never")}</option>
        </select>
        <span />
        <div className="mt-row"><button className="btn primary small" onClick={async () => setMade(await shareLink(app, {
          kind: "snapshot", title: title.trim(), server_id: ctx.serverId, database_id: ctx.dbId, expires_days: days ? Number(days) : null,
          snapshot: { sql: ctx.sql ?? "", columns: ctx.columns, rows: ctx.rows, row_count: ctx.rows.length, truncated: ctx.hasMore },
        }))}>{t("Create and copy the link")}</button></div>
      </div>
      {made && <div className="mt-row"><input className="text lk-url" readOnly value={fullUrl(made.path)} aria-label={t("Link")} onFocus={(e) => e.currentTarget.select()} /></div>}
    </div>
  );
}

function LinksTab(props: { visible: boolean }) {
  const app = useApp();
  const [all, setAll] = useState(false);
  const [data, setData] = useState<{ links: LinkInfo[]; server_mode: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => linkApi.list(all).then((d) => { setData(d); setError(null); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { if (props.visible) load(); }, [props.visible, all]);
  const target = (l: LinkInfo) => {
    const db = app.servers.find((s) => s.id === l.server_id)?.databases.find((d) => d.id === l.database_id)?.name;
    if (l.kind === "object") return `${l.name}${db ? ` · ${db}` : ""}`;
    if (l.kind === "snapshot") return t("{n} row(s)", { n: l.rows ?? 0 }) + (db ? ` · ${db}` : "");
    return l.item_id ?? "";
  };
  return (
    <div className="lk-list">
      <div className="lk-head">
        <h3>{t("Shared links")}</h3>
        <span style={{ flex: 1 }} />
        <label className="check"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> {t("Everyone's links (administrators)")}</label>
      </div>
      <p className="muted">{t("Links open with the permissions of whoever opens them. Make one from an object's context menu, a result's Share view, a saved query or a notebook.")}</p>
      {error && <div className="err">{error}</div>}
      {data && data.links.length === 0 && <div className="muted">{t("No links yet.")}</div>}
      {data && data.links.length > 0 && <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Shared links")}><table className="grid">
        <thead><tr><th>{t("Title")}</th><th>{t("Kind")}</th><th>{t("Target")}</th><th>{t("By")}</th><th>{t("Created")}</th><th>{t("Expires")}</th><th>{t("Opened")}</th><th /></tr></thead>
        <tbody>{data.links.map((l) => (
          <tr key={l.id}>
            <td>{l.title || <span className="muted">—</span>}</td>
            <td>{t(KIND[l.kind])}</td>
            <td>{target(l)}</td>
            <td>{l.created_by ?? "—"}</td>
            <td>{fmtTime(l.created_at)}</td>
            <td>{l.expires_at ? fmtTime(l.expires_at) : t("never")}</td>
            <td className="num">{l.opened}</td>
            <td className="lk-acts">
              <button className="btn small" onClick={() => app.showDialog(<LinkDialog url={fullUrl(l.path)} onClose={() => app.showDialog(null)} />)}>{t("Show link")}</button>
              <button className="btn small" onClick={() => openLink(app, l.id)}>{t("Open")}</button>
              <button className="btn small danger" onClick={async () => {
                if (!confirm(t("Revoke this link? It stops working for everyone."))) return;
                try { await linkApi.revoke(l.id); load(); } catch (e) { app.toast(errMsg(e), "error"); }
              }}>{t("Revoke link")}</button>
            </td>
          </tr>
        ))}</tbody>
      </table></div>}
    </div>
  );
}

registerTabKind("link-snapshot", {
  icon: <span aria-hidden="true">🔗</span>,
  render: (tab) => <SnapshotTab link={tab.props!.link} />,
  same: (a, b) => a.kind === b.kind && a.props?.link?.id === b.props?.link?.id,
});
registerTabKind("links", {
  icon: <span aria-hidden="true">🔗</span>,
  render: (_tab, visible) => <LinksTab visible={visible} />,
  same: (a, b) => a.kind === b.kind,
});

registerResultView({ id: "share", title: N_("Share"), icon: <span aria-hidden="true">🔗</span>, render: (ctx) => <ShareResult ctx={ctx} /> });

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "object" || !sel.dbId || !sel.otype || !sel.name) return [];
  const { serverId, dbId, otype, name, schema } = sel;
  return [{ label: t("Copy link"), onClick: () => shareLink(app, { kind: "object", title: name, server_id: serverId, database_id: dbId, otype, name, schema: schema ?? null }) }];
});

export function openLinks(app: AppApi) {
  app.openTab({ kind: "links", title: t("Shared links") });
}

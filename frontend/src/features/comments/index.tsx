/** Roadmap 2.4: comments on objects and notebooks - short threads shown where the object is. */
import { useEffect, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import { N_, t } from "../../i18n";
import { registerObjectTab } from "../../registry";
import type { Selection } from "../../types";
import "./comments.css";

export interface CommentTarget {
  kind: "object" | "notebook"; server_id?: string; database_id?: string; otype?: string; name?: string; schema?: string | null;
  notebook_id?: string;
}
export interface CommentItem {
  id: string; target: CommentTarget; text: string; author: string | null; created_at: number; edited_at: number | null;
  mine: boolean; can_delete: boolean;
}
const qs = (tg: CommentTarget) => new URLSearchParams(Object.entries(tg).filter(([, v]) => v != null && v !== "").map(([k, v]) => [k, String(v)])).toString();
export const commentsApi = {
  list: (tg: CommentTarget) => request<CommentItem[]>("GET", `/api/comments?${qs(tg)}`),
  add: (tg: CommentTarget, text: string) => request<CommentItem>("POST", "/api/comments", { ...tg, text }),
  edit: (id: string, text: string) => request<CommentItem>("PUT", `/api/comments/${encodeURIComponent(id)}`, { text }),
  remove: (id: string) => request<void>("DELETE", `/api/comments/${encodeURIComponent(id)}`),
};

const when = (ts: number) => new Date(ts * 1000).toLocaleString();

/** The thread of one object or notebook, with a box to add a comment. */
export function CommentThread(props: { target: CommentTarget; label: string; onCount?: (n: number) => void }) {
  const app = useApp();
  const [items, setItems] = useState<CommentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const key = qs(props.target);
  const load = () => commentsApi.list(props.target).then((x) => { setItems(x); setError(null); props.onCount?.(x.length); }).catch((e) => setError(errMsg(e)));
  useEffect(() => { setItems(null); load(); }, [key]);
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); await load(); } catch (e) { app.toast(errMsg(e), "error"); } };
  if (error) return <div className="err">{error}</div>;
  return (
    <div className="cmt" role="region" aria-label={props.label}>
      {items === null ? <div className="muted">{t("Loading…")}</div> : items.length === 0
        ? <div className="muted">{t("No comments yet. Leave a note for the next person: why it exists, who uses it, what not to change.")}</div>
        : <ol className="cmt-list">{items.map((c) => (
          <li key={c.id} className="cmt-item">
            <div className="cmt-meta"><strong>{c.author ?? t("you")}</strong> <span className="muted">{when(c.created_at)}{c.edited_at ? ` · ${t("edited")}` : ""}</span>
              <span className="cmt-actions">
                {c.mine && editing?.id !== c.id && <button className="btn small" onClick={() => setEditing({ id: c.id, text: c.text })}>{t("Edit")}</button>}
                {c.can_delete && <button className="btn small danger" onClick={() => window.confirm(t("Delete this comment?")) && act(() => commentsApi.remove(c.id))}>{t("Delete")}</button>}
              </span>
            </div>
            {editing?.id === c.id ? (
              <div className="cmt-edit">
                <textarea className="text" rows={3} aria-label={t("Edit the comment")} value={editing.text} maxLength={4000} onChange={(e) => setEditing({ id: c.id, text: e.target.value })} />
                <div className="mt-row">
                  <button className="btn small primary" disabled={!editing.text.trim()} onClick={() => act(async () => { await commentsApi.edit(c.id, editing.text); setEditing(null); })}>{t("Save")}</button>
                  <button className="btn small" onClick={() => setEditing(null)}>{t("Cancel")}</button>
                </div>
              </div>
            ) : <div className="cmt-text">{c.text}</div>}
          </li>
        ))}</ol>}
      <div className="cmt-new">
        <textarea className="text" rows={2} aria-label={t("New comment")} placeholder={t("Add a comment…")} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && text.trim()) act(async () => { await commentsApi.add(props.target, text.trim()); setText(""); }); }} />
        <button className="btn small primary" disabled={!text.trim()} onClick={() => act(async () => { await commentsApi.add(props.target, text.trim()); setText(""); })}>{t("Comment@@verb")}</button>
      </div>
    </div>
  );
}

function ObjectComments(props: { sel: Selection }) {
  const s = props.sel;
  return (
    <div className="pad cmt-tab">
      <CommentThread label={t("Comments on {name}", { name: s.name ?? "" })}
        target={{ kind: "object", server_id: s.serverId, database_id: s.dbId, otype: s.otype, name: s.name, schema: s.schema ?? null }} />
    </div>
  );
}

registerObjectTab({
  id: "comments",
  title: N_("Comments"),
  applies: (sel) => !!sel && sel.kind === "object" && !!sel.dbId && !!sel.otype && !!sel.name,
  render: (sel) => <ObjectComments sel={sel} />,
});

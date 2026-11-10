/** Roadmap 2.4: change requests with approval - propose DDL or migrations, dry run with the schema diff, approve,
 * apply, with the request's history as its audit trail. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Selection } from "../../types";
import { N_, t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { SqlEditor } from "../../components/SqlEditor";
import { IconPlus, IconRefresh } from "../../components/Icons";
import { Section } from "../maintenance/common";
import { fmtTime, waitJob } from "../maintenance/api";
import { SideBySide } from "../schema/common";
import "../maintenance/maintenance.css";
import "./changes.css";
import { crApi, type ChangeRequest, type ChangeStatus, type ScriptItem } from "./api";

const STATUS_LABEL: Record<ChangeStatus, string> = {
  open: N_("open"), approved: N_("approved"), rejected: N_("rejected"), withdrawn: N_("withdrawn"),
  applying: N_("applying"), applied: N_("applied"), failed: N_("failed"),
};
const STATUS_PILL: Record<ChangeStatus, string> = {
  open: "running", approved: "finished", rejected: "failed", withdrawn: "cancelled", applying: "running", applied: "finished", failed: "failed",
};
const ACTION_LABEL: Record<string, string> = {
  submitted: N_("proposed the change"), revised: N_("revised the change"), edited: N_("edited the description"),
  comment: N_("commented"), dry_run: N_("ran the dry run"), approved: N_("approved"), rejected: N_("rejected"),
  withdrawn: N_("withdrew the request"), applying: N_("started applying"), applied: N_("applied the change"), failed: N_("applying failed"),
};
const CHANGE_LABEL: Record<string, string> = { created: N_("created"), altered: N_("altered"), dropped: N_("dropped") };
export const statusPill = (s: ChangeStatus) => <span className={"pill " + STATUS_PILL[s]}>{t(STATUS_LABEL[s])}</span>;

function NewRequest(props: { sid: string; dbid: string; onDone: (cr: ChangeRequest | null) => void }) {
  const app = useApp();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState<"sql" | "migrations">("sql");
  const [sql, setSql] = useState("");
  const [scripts, setScripts] = useState<ScriptItem[]>([]);
  const [busy, setBusy] = useState(false);
  const addFiles = async (files: FileList | null) => {
    const read = await Promise.all(Array.from(files ?? []).map(async (f) => ({ name: f.name, sql: await f.text() })));
    setScripts((cur) => [...cur.filter((s) => !read.some((r) => r.name === s.name)), ...read].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })));
  };
  return (
    <Section wide title={t("New change request")} hint={t("Propose a change to this database. An administrator of the database runs a dry run on a copy, reviews the schema changes and approves it; then fbadmin applies it.")}>
      <div className="form mform">
        <label className="l" htmlFor="cr-title">{t("Title")}</label>
        <input id="cr-title" className="text" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder={t("What the change is for")} />
        <label className="l" htmlFor="cr-desc">{t("Description")}</label>
        <textarea id="cr-desc" className="text" rows={3} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t("Why, and anything the reviewer should know (optional)")} />
        <label className="l" htmlFor="cr-kind">{t("Change")}</label>
        <select id="cr-kind" className="text" value={kind} onChange={(e) => setKind(e.target.value as "sql" | "migrations")}>
          <option value="sql">{t("a SQL script")}</option>
          <option value="migrations">{t("versioned migration scripts (V<version>__<description>.sql)")}</option>
        </select>
      </div>
      {kind === "sql" ? (
        <div className="cr-editor" aria-label={t("SQL of the change")}><SqlEditor value={sql} onChange={setSql} placeholder={t("ALTER TABLE …;")} /></div>
      ) : (
        <div className="cr-files">
          <label className="btn small">{t("Add script files…")}<input type="file" accept=".sql" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} /></label>
          {scripts.length === 0 ? <span className="muted">{t("No scripts yet. Scripts already applied to the database are skipped.")}</span> : (
            <ul>{scripts.map((s) => <li key={s.name}><span className="mono">{s.name}</span> <span className="muted">{t("{n} line(s)", { n: s.sql.split("\n").length })}</span>{" "}
              <button className="btn small" onClick={() => setScripts(scripts.filter((x) => x !== s))}>{t("Remove")}</button></li>)}</ul>
          )}
        </div>
      )}
      <div className="mt-row" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={busy || !title.trim() || (kind === "sql" ? !sql.trim() : scripts.length === 0)} onClick={async () => {
          setBusy(true);
          try {
            const cr = await crApi.create({ server_id: props.sid, database_id: props.dbid, title: title.trim(), description, kind, sql: kind === "sql" ? sql : "", scripts: kind === "migrations" ? scripts : [] });
            app.toast(t("Change request #{n} proposed", { n: cr.number }), "ok");
            props.onDone(cr);
          } catch (e) { app.toast(errMsg(e), "error"); }
          setBusy(false);
        }}>{t("Propose the change")}</button>
        <button className="btn" onClick={() => props.onDone(null)}>{t("Cancel")}</button>
      </div>
    </Section>
  );
}

function Detail(props: { id: string; onChanged: () => void }) {
  const app = useApp();
  const [cr, setCr] = useState<ChangeRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [copy, setCopy] = useState<"metadata" | "full">("metadata");
  const load = async () => { try { setCr(await crApi.get(props.id)); setError(null); } catch (e) { setError(errMsg(e)); } };
  useEffect(() => { setCr(null); setEditing(null); setNote(""); load(); }, [props.id]);
  const act = async (fn: () => Promise<ChangeRequest>, ok?: string) => {
    try { setCr(await fn()); setNote(""); if (ok) app.toast(ok, "ok"); props.onChanged(); } catch (e) { app.toast(errMsg(e), "error"); }
  };
  const job = async (label: string, start: () => Promise<{ id: string }>) => {
    try {
      setRunning(label);
      const end = await waitJob((await start()).id, (j) => setRunning(`${label} ${j.log?.length ? j.log[j.log.length - 1] : ""}`));
      if (end.status !== "finished") app.toast(end.error ?? t("The job failed"), "error");
    } catch (e) { app.toast(errMsg(e), "error"); }
    setRunning(null);
    await load();
    props.onChanged();
  };
  if (error) return <div className="err">{error}</div>;
  if (!cr) return <div className="muted">{t("Loading…")}</div>;
  const d = cr.dry_run;
  const text = cr.kind === "sql" ? cr.sql ?? "" : (cr.scripts ?? []).map((s) => `-- ${s.name}\n${s.sql}`).join("\n");
  return (
    <div className="cr-detail">
      <Section wide title={<>{`#${cr.number} ${cr.title}`} {statusPill(cr.status)}</>}
        hint={t("Proposed by {who} on {time} · revision {rev} · {n} statement(s)", { who: cr.author ?? t("you"), time: fmtTime(cr.created_at), rev: cr.revision, n: cr.statements })}
        right={<button className="btn small" onClick={load} aria-label={t("Refresh")}><IconRefresh size={12} /></button>}>
        {cr.description && <p className="cr-desc">{cr.description}</p>}
        <div className="mt-row cr-actions">
          {cr.can.dry_run && <>
            <select className="text" aria-label={t("Copy for the dry run")} value={copy} onChange={(e) => setCopy(e.target.value as "metadata" | "full")}>
              <option value="metadata">{t("a metadata-only copy (fast)")}</option>
              <option value="full">{t("a full copy with the data (catches data errors)")}</option>
            </select>
            <button className="btn small" disabled={!!running} onClick={() => job(t("Dry run…"), () => crApi.dryRun(cr.id, copy))}>{d ? t("Run the dry run again") : t("Run the dry run")}</button>
          </>}
          {cr.can.apply && <button className="btn primary small" disabled={!!running} onClick={() => {
            if (window.confirm(t("Apply change request #{n} to {db} now?", { n: cr.number, db: cr.database ?? "" }))) job(t("Applying…"), () => crApi.apply(cr.id));
          }}>{t("Apply the change")}</button>}
          {cr.can.edit && editing === null && <button className="btn small" onClick={() => setEditing(text)} disabled={cr.kind !== "sql"} title={cr.kind !== "sql" ? t("Propose the new scripts as a new request") : undefined}>{t("Edit")}</button>}
          {cr.can.withdraw && <button className="btn small" onClick={() => window.confirm(t("Withdraw change request #{n}?", { n: cr.number })) && act(() => crApi.withdraw(cr.id), t("Withdrawn"))}>{t("Withdraw")}</button>}
        </div>
        {running && <div className="muted" role="status">{running}</div>}
        <div className="cr-approvals">
          <strong>{t("Approvals: {n} of {m}", { n: cr.approvals.length, m: cr.approvals_needed })}</strong>
          {cr.approvals.map((a, i) => <div key={i}>✓ {t("{who} on {time}", { who: a.by ?? t("you"), time: fmtTime(a.at) })}{a.comment ? `: ${a.comment}` : ""}</div>)}
          {cr.status === "open" && cr.approve_blocked && <div className="muted">{cr.approve_blocked}</div>}
        </div>
        {(cr.can.approve || cr.can.reject) && (
          <div className="cr-review">
            <label htmlFor="cr-note">{t("Review comment")}</label>
            <textarea id="cr-note" className="text" rows={2} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("Required to reject: the author reads it")} />
            <div className="mt-row">
              {cr.can.approve && <button className="btn primary small" onClick={() => act(() => crApi.approve(cr.id, note), t("Approved"))}>{t("Approve")}</button>}
              {cr.can.reject && <button className="btn small danger" disabled={!note.trim()} onClick={() => act(() => crApi.reject(cr.id, note), t("Rejected"))}>{t("Reject")}</button>}
            </div>
          </div>
        )}
        {cr.applied && <div className={cr.applied.ok ? "alerts" : "err"}>
          {cr.applied.ok ? <div className="alert good"><span className="lvl">{t("applied")}</span><span>{t("Applied by {who} on {time}", { who: cr.applied.by ?? t("you"), time: fmtTime(cr.applied.at) })}</span></div>
            : t("Applying failed: {error}", { error: cr.applied.error ?? "" })}
        </div>}
      </Section>

      <Section wide title={t("The change")}>
        {editing !== null ? <>
          <div className="cr-editor"><SqlEditor value={editing} onChange={setEditing} /></div>
          <div className="mt-row" style={{ marginTop: 8 }}>
            <button className="btn primary small" onClick={() => act(() => crApi.edit(cr.id, { sql: editing }), t("Saved")).then(() => setEditing(null))}>{t("Save the revision")}</button>
            <button className="btn small" onClick={() => setEditing(null)}>{t("Cancel")}</button>
            <span className="muted">{t("Saving a new revision clears the approvals and needs a new dry run.")}</span>
          </div>
        </> : <div className="cr-editor ro"><SqlEditor value={text} readOnly /></div>}
      </Section>

      {d && <Section wide title={<>{t("Dry run")} <span className={"pill " + (d.ok ? "finished" : "failed")}>{d.ok ? t("passed") : t("failed")}</span></>}
        hint={t("{time} by {who} on a {copy} copy, {n} s; the copy was dropped", { time: fmtTime(d.finished_at), who: d.by ?? t("you"), copy: d.copy === "full" ? t("full") : t("metadata-only"), n: d.seconds })}>
        {d.stale && <div className="err">{t("The change was edited after this dry run: run it again.")}</div>}
        {d.scripts.filter((s) => !s.ok).map((s) => <div key={s.script} className="err">{t("{script}, line {line}: {message}", { script: s.script, line: s.error?.line ?? 0, message: s.error?.message ?? "" })}</div>)}
        <p className="muted">{t("{created} created, {altered} altered, {dropped} dropped", { created: d.summary.created ?? 0, altered: d.summary.altered ?? 0, dropped: d.summary.dropped ?? 0 })}</p>
        <ul className="cr-changes" aria-label={t("Schema changes")}>{d.changes.map((c, i) => (
          <li key={i}><details open={d.changes.length <= 3}>
            <summary><span className={"cr-chg " + c.change}>{t(CHANGE_LABEL[c.change])}</span> {c.type} <span className="mono">{c.schema ? `${c.schema}.` : ""}{c.name}</span></summary>
            <SideBySide left={c.before} right={c.after} leftTitle={t("Now")} rightTitle={t("After the change")} />
          </details></li>
        ))}</ul>
      </Section>}

      <Section wide title={t("History")} hint={t("Every step of the request, also kept in the audit log and the database's timeline.")}>
        <ol className="cr-history">{(cr.history ?? []).map((e, i) => (
          <li key={i} className={e.action}>
            <span className="muted">{fmtTime(e.at)}</span> <strong>{e.by ?? t("you")}</strong> {t(ACTION_LABEL[e.action] ?? e.action)}
            {e.detail && <div className={e.action === "comment" || e.action === "rejected" || e.action === "approved" ? "cr-comment" : "muted"}>{e.detail}</div>}
          </li>
        ))}</ol>
        <div className="cr-review">
          <label htmlFor="cr-comment">{t("Comment")}</label>
          <CommentBox onSend={(text) => act(() => crApi.comment(cr.id, text))} />
        </div>
      </Section>
    </div>
  );
}

function CommentBox(props: { onSend: (text: string) => Promise<void> }) {
  const [text, setText] = useState("");
  return (
    <div className="mt-row">
      <textarea id="cr-comment" className="text" rows={2} style={{ flex: 1 }} value={text} onChange={(e) => setText(e.target.value)} />
      <button className="btn small" disabled={!text.trim()} onClick={async () => { await props.onSend(text.trim()); setText(""); }}>{t("Send")}</button>
    </div>
  );
}

function ChangesTab(props: { serverId: string; dbId: string; visible: boolean; openId?: string }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [items, setItems] = useState<ChangeRequest[]>([]);
  const [filter, setFilter] = useState<"active" | "">("active");
  const [selected, setSelected] = useState<string | null>(props.openId ?? null);
  const [creating, setCreating] = useState(false);
  const load = async () => { try { setItems(await crApi.list(dbid, filter || undefined)); } catch (e) { app.toast(errMsg(e), "error"); } };
  useEffect(() => { if (props.visible) load(); }, [props.visible, filter]);
  return (
    <div className="mt-body cr">
      <div className="cr-side">
        <div className="mt-row">
          <button className="btn primary small" onClick={() => { setCreating(true); setSelected(null); }}><IconPlus size={12} /> {t("New change request")}</button>
          <select className="text" aria-label={t("Show")} value={filter} onChange={(e) => setFilter(e.target.value as "active" | "")}>
            <option value="active">{t("open and approved")}</option>
            <option value="">{t("all")}</option>
          </select>
        </div>
        {items.length === 0 ? <div className="muted">{filter ? t("No open change requests.") : t("No change requests yet.")}</div> : (
          <ul className="cr-list" aria-label={t("Change requests")}>{items.map((c) => (
            <li key={c.id}><button className={"cr-item" + (selected === c.id ? " sel" : "")} aria-current={selected === c.id} onClick={() => { setSelected(c.id); setCreating(false); }}>
              <span className="cr-num">#{c.number}</span> <span className="cr-ttl">{c.title}</span> {statusPill(c.status)}
              <span className="muted cr-meta">{t("{who}, {time}", { who: c.author ?? t("you"), time: fmtTime(c.created_at) })}</span>
            </button></li>
          ))}</ul>
        )}
      </div>
      <div className="cr-main">
        {creating ? <NewRequest sid={sid} dbid={dbid} onDone={(cr) => { setCreating(false); if (cr) { setSelected(cr.id); load(); } }} />
          : selected ? <Detail id={selected} onChanged={load} />
            : <div className="muted cr-empty">{t("Pick a change request, or propose a new one.")}</div>}
      </div>
    </div>
  );
}

export function openChanges(app: AppApi, sid: string, dbid: string, openId?: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "changes", title: t("{db} · change requests", { db }), serverId: sid, dbId: dbid, props: { openId } });
}

registerTabKind("changes", {
  icon: <span aria-hidden="true" style={{ fontSize: 12 }}>⇄</span>,
  render: (tab, visible) => <ChangesTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} openId={(tab.props as { openId?: string } | undefined)?.openId} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Change requests…"), onClick: () => openChanges(app, sid, dbid) }];
});

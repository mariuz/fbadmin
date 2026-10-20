import { Fragment, useEffect, useMemo, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { SqlEditor } from "../../components/SqlEditor";
import { IconCopy, IconDownload, IconPlay, IconRefresh, IconSpinner, IconTerminal, IconUpload } from "../../components/Icons";
import { isAdmin, useAuth } from "../security/state";
import { historyApi, type Commit, type CommitDetail, type CommitFile, type ConfigResponse, type HistoryConfig, type MigrationResult } from "./api";
import { N_, t } from "../../i18n";

const DEFAULTS: HistoryConfig = {
  repo_path: "", subdir: "", branch: "main", remote_url: "", push: true, token: "", token_user: "x-access-token",
  committer_name: "fbadmin", committer_email: "fbadmin@localhost", after_ddl: false,
};
const STATUS_SIGN: Record<string, string> = { added: "+", changed: "≠", dropped: "−" };
const STATUS_CLASS: Record<string, string> = { added: "create", changed: "alter", dropped: "drop" };
const STATUS_LABEL: Record<string, string> = { added: N_("Added"), changed: N_("Changed"), dropped: N_("Dropped") };

const fmtTime = (s: number) => new Date(s * 1000).toLocaleString();
const short = (sha: string | null | undefined) => (sha ?? "").slice(0, 10);

function safeName(s: string) {
  return (s || "database").replace(/[^\w.-]+/g, "_");
}

// ------------------------------------------------------------------ diff with light SQL colouring
const KW = /\b(CREATE|ALTER|DROP|RECREATE|TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER|PACKAGE|BODY|DOMAIN|SEQUENCE|GENERATOR|EXCEPTION|INDEX|ROLE|GRANT|REVOKE|ON|TO|FROM|AS|BEGIN|END|DECLARE|VARIABLE|RETURNS|RETURN|SELECT|INSERT|UPDATE|DELETE|INTO|WHERE|AND|OR|NOT|NULL|DEFAULT|PRIMARY|FOREIGN|KEY|REFERENCES|UNIQUE|CHECK|CONSTRAINT|COMMENT|IS|ADD|COMPUTED|BY|GENERATED|IDENTITY|ACTIVE|INACTIVE|BEFORE|AFTER|POSITION|FOR|IF|THEN|ELSE|SUSPEND|EXIT|DO|WHILE|COLLATE|CHARACTER|SET|CASCADE|ASCENDING|DESCENDING|USING|INTEGER|BIGINT|SMALLINT|VARCHAR|CHAR|NUMERIC|DECIMAL|DOUBLE|PRECISION|DATE|TIME|TIMESTAMP|BOOLEAN|BLOB|SUB_TYPE|TEXT)\b/g;

function SqlLine({ text }: { text: string }) {
  // strings and comments first, then keywords in the remaining code
  const parts: Array<{ s: string; cls?: string }> = [];
  const re = /('(?:[^']|'')*'?)|(--.*$)|(\/\*.*?(?:\*\/|$))/g;
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) parts.push({ s: text.slice(last, m.index) });
    parts.push({ s: m[0], cls: m[1] ? "hist-str" : "hist-cm" });
    last = m.index + m[0].length;
    if (!m[0].length) re.lastIndex++;
  }
  if (last < text.length) parts.push({ s: text.slice(last) });
  return (
    <>
      {parts.map((p, i) => p.cls ? <span key={i} className={p.cls}>{p.s}</span> : (
        <Fragment key={i}>{p.s.split(KW).map((x, j) => (j % 2 ? <span key={j} className="hist-kw">{x}</span> : x))}</Fragment>
      ))}
    </>
  );
}

function UnifiedDiff({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <div className="hist-diff" role="region" aria-label={t("Changes")} tabIndex={0}>
      {lines.map((ln, i) => {
        const kind = ln.startsWith("@@") ? "hunk" : ln.startsWith("+") ? "add" : ln.startsWith("-") ? "del" : ln.startsWith("\\") ? "note" : "same";
        return (
          <div key={i} className={"hist-line hist-" + kind}>
            <span className="hist-sign" aria-hidden="true">{kind === "add" ? "+" : kind === "del" ? "−" : " "}</span>
            <span className="hist-code">{kind === "hunk" || kind === "note" ? ln : <SqlLine text={ln.slice(1)} />}</span>
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ configuration form
function ConfigForm(props: { sid: string; dbid: string; initial: HistoryConfig | null; status: ConfigResponse["status"]; onSaved: (r: ConfigResponse) => void; onCancel?: () => void }) {
  const [f, setF] = useState<HistoryConfig>({ ...DEFAULTS, ...(props.initial ?? {}), remote_url: props.initial?.remote_url ?? "", token: props.initial?.token ?? "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof HistoryConfig>(k: K, v: HistoryConfig[K]) => setF({ ...f, [k]: v });
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      props.onSaved(await historyApi.save(props.sid, props.dbid, { ...f, remote_url: f.remote_url?.trim() || null, token: f.token?.trim() || null }));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const field = (id: string, label: string, el: React.ReactNode, hint?: string) => (
    <>
      <label className="l" htmlFor={id}>{label}</label>
      <div>{el}{hint && <div className="muted hist-hint">{hint}</div>}</div>
    </>
  );
  return (
    <section className="hist-config" aria-labelledby="hist-config-title">
      <h3 id="hist-config-title">{t("Repository settings")}</h3>
      <div className="form">
        {field("hist-path", t("Repository folder"), <input id="hist-path" className="text mono" value={f.repo_path} placeholder="customers-db" onChange={(e) => set("repo_path", e.target.value)} />,
          t("A folder on the fbadmin host. Relative paths are inside {root}; it is created with git init when missing.", { root: props.status.root }))}
        {field("hist-sub", t("Sub-folder"), <input id="hist-sub" className="text mono" value={f.subdir} placeholder={t("(repository root)")} onChange={(e) => set("subdir", e.target.value)} />,
          t("Optional: keep several databases in one repository. Other files in the repository are left alone."))}
        {field("hist-branch", t("Branch"), <input id="hist-branch" className="text mono" value={f.branch} onChange={(e) => set("branch", e.target.value)} />)}
        {field("hist-remote", t("Remote URL"), <input id="hist-remote" className="text mono" value={f.remote_url ?? ""} placeholder="git@github.com:company/schema.git" onChange={(e) => set("remote_url", e.target.value)} />,
          t("Optional. Pushes use the host's SSH keys or git credential helper, or the token below. Never put a password in the URL."))}
        {f.remote_url?.trim() && <>
          {field("hist-token", t("HTTPS token"), <input id="hist-token" className="text mono" value={f.token ?? ""} placeholder="env:GIT_TOKEN" onChange={(e) => set("token", e.target.value)} />,
            t("Only as env:NAME: the token is read from that environment variable of the fbadmin process when pushing."))}
          {field("hist-token-user", t("Token user name"), <input id="hist-token-user" className="text mono" value={f.token_user} onChange={(e) => set("token_user", e.target.value)} />)}
          <span /><label className="check"><input type="checkbox" checked={f.push} onChange={(e) => set("push", e.target.checked)} /> {t("Push after each commit")}</label>
        </>}
        {field("hist-cname", t("Committer name"), <input id="hist-cname" className="text" value={f.committer_name} onChange={(e) => set("committer_name", e.target.value)} />)}
        {field("hist-cmail", t("Committer e-mail"), <input id="hist-cmail" className="text" value={f.committer_email} onChange={(e) => set("committer_email", e.target.value)} />,
          props.status.server_mode ? t("The fbadmin user who takes a snapshot is recorded as the commit's author.") : undefined)}
        <span /><label className="check"><input type="checkbox" checked={f.after_ddl} onChange={(e) => set("after_ddl", e.target.checked)} /> {t("Take a snapshot after DDL run through fbadmin")}</label>
        <span /><div className="muted hist-hint">{t("A few seconds after the DDL is committed, in the background. Scheduled snapshots: Maintenance → Scheduled jobs → “Schema snapshot”.")}</div>
      </div>
      {error && <div className="err" role="alert" style={{ marginTop: 8 }}>{error}</div>}
      <div className="hist-actions">
        <button className="btn primary" disabled={busy || !f.repo_path.trim()} onClick={save}>{t("Save")}</button>
        {props.onCancel && <button className="btn" onClick={props.onCancel}>{t("Cancel")}</button>}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ main tab
type Pane = { kind: "diff" } | { kind: "file"; title: string; content: string } | { kind: "migration"; from: string; result: MigrationResult };

export function HistoryTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const auth = useAuth();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const dbName = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? "database";
  const [cfg, setCfg] = useState<ConfigResponse | null>(null);
  const [editing, setEditing] = useState(false);
  const [commits, setCommits] = useState<Commit[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<CommitDetail | null>(null);
  const [file, setFile] = useState<CommitFile | null>(null);
  const [pane, setPane] = useState<Pane>({ kind: "diff" });
  const [fromSha, setFromSha] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const canEdit = !cfg?.status.server_mode || isAdmin(auth);

  const loadLog = async (keepSel = true) => {
    const r = await historyApi.log(sid, dbid, 100);
    setCommits(r.commits);
    setHasMore(r.has_more);
    if (!keepSel || !r.commits.some((c) => c.sha === sel)) setSel(r.commits[0]?.sha ?? null);
  };
  const load = async () => {
    setError(null);
    try {
      const c = await historyApi.get(sid, dbid);
      setCfg(c);
      if (c.config) {
        try { await loadLog(); } catch { setCommits([]); }  // no repository before the first snapshot
      }
    } catch (e) {
      setError(errMsg(e));
    }
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    setDetail(null);
    setFile(null);
    setPane({ kind: "diff" });
    if (!sel) return;
    historyApi.commit(sid, dbid, sel).then((d) => {
      setDetail(d);
      setFile(d.files.find((f) => f.object) ?? d.files[0] ?? null);
      setFromSha(d.parent ?? "");
    }).catch((e) => setError(errMsg(e)));
  }, [sel]);

  const act = async (name: string, fn: () => Promise<void>) => {
    setBusy(name);
    setError(null);
    try { await fn(); } catch (e) { setError(errMsg(e)); } finally { setBusy(null); }
  };
  const snapshot = () => act("snapshot", async () => {
    const r = await historyApi.snapshot(sid, dbid);
    if (!r.committed) app.toast(t("No schema changes since the last snapshot"), "info");
    else {
      const n = r.changes.added.length + r.changes.changed.length + r.changes.dropped.length;
      app.toast(t("Snapshot committed: {n} object(s) changed", { n }), "ok");
      if (r.push && !r.push.ok) app.toast(t("Committed, but the push failed: {error}", { error: r.push.error ?? "" }), "error");
    }
    setCfg(await historyApi.get(sid, dbid));
    await loadLog(false);
  });
  const push = () => act("push", async () => {
    await historyApi.push(sid, dbid);
    app.toast(t("Pushed"), "ok");
    setCfg(await historyApi.get(sid, dbid));
  });
  const show = (sha: string, f: CommitFile, before: boolean) => act("file", async () => {
    const r = await historyApi.file(sid, dbid, sha, f.path);
    const name = f.object?.name ? `${f.object.label} ${f.object.name}` : f.path;
    setPane({ kind: "file", content: r.content, title: before ? t("{name} before this commit ({sha})", { name, sha: short(sha) }) : t("{name} at commit {sha}", { name, sha: short(sha) }) });
  });
  const migrate = () => act("migration", async () => {
    if (!sel || !fromSha) return;
    setPane({ kind: "migration", from: fromSha, result: await historyApi.migration(sid, dbid, fromSha, sel) });
  });
  const remove = () => act("remove", async () => {
    if (!confirm(t("Stop keeping the schema history of this database? The repository folder is not deleted."))) return;
    await historyApi.remove(sid, dbid);
    setCommits([]);
    setSel(null);
    await load();
  });

  const objectFiles = useMemo(() => detail?.files ?? [], [detail]);
  const c = cfg?.config;
  const st = cfg?.status;

  if (!cfg) return <div className="sch-col">{error ? <div className="pad err" role="alert">{error}</div> : <div className="empty-state"><IconSpinner /></div>}</div>;

  if (!c || editing) {
    return (
      <div className="sch-col hist">
        {!st?.git_available && <div className="hist-banner err" role="alert">{st?.error}</div>}
        <div className="hist-scroll">
          {!c && <div className="hist-intro">
            <h2>{t("Schema history in git")}</h2>
            <p>{t("fbadmin writes the schema of {db} into a git repository, one file per table, view, procedure and other object, and commits whenever something changed. The history shows who changed what and when, with diffs, and can generate a migration script between any two snapshots.", { db: dbName })}</p>
          </div>}
          {canEdit ? <ConfigForm sid={sid} dbid={dbid} initial={c ?? null} status={st!} onCancel={c ? () => setEditing(false) : undefined}
            onSaved={(r) => { setCfg(r); setEditing(false); load(); }} />
            : <div className="hist-intro muted">{t("Schema history is not set up for this database. An fbadmin administrator can configure it.")}</div>}
          {error && <div className="pad err" role="alert">{error}</div>}
        </div>
      </div>
    );
  }

  const selCommit = commits.find((x) => x.sha === sel);
  return (
    <div className="sch-col hist">
      <div className="toolbar">
        <button className="btn primary" disabled={!!busy || !st?.git_available} onClick={snapshot}>{busy === "snapshot" ? <IconSpinner /> : <IconPlay />} {t("Take snapshot")}</button>
        <button className="btn" onClick={() => load()} aria-label={t("Refresh")} title={t("Refresh")}><IconRefresh /></button>
        {c.remote_url && <button className="btn" disabled={!!busy} onClick={push}>{busy === "push" ? <IconSpinner /> : <IconUpload />} {t("Push")}</button>}
        {canEdit && <button className="btn" onClick={() => setEditing(true)}>{t("Settings")}</button>}
        {canEdit && <button className="btn" disabled={!!busy} onClick={remove}>{t("Stop history")}</button>}
        <span className="grow" />
        <span className="muted hist-repo" title={c.resolved_path}>{c.resolved_path}{c.subdir ? `/${c.subdir}` : ""} · {c.branch}{c.after_ddl ? " · " + t("after DDL") : ""}</span>
      </div>
      {!st?.git_available && <div className="hist-banner err" role="alert">{st?.error}</div>}
      {c.last_error && <div className="hist-banner err" role="alert">{t("Last snapshot failed: {error}", { error: c.last_error })}</div>}
      {c.last_push_error && <div className="hist-banner warn" role="alert">{t("Last push failed: {error}", { error: c.last_push_error })}</div>}
      {error && <div className="hist-banner err" role="alert">{error}</div>}
      {commits.length === 0 ? (
        <div className="empty-state"><div className="big">{t("No snapshots yet")}</div><div>{t("Take a snapshot to start the history.")}</div></div>
      ) : (
        <div className="sch-split">
          <nav className="hist-commits" aria-label={t("Commits")}>
            <ul>
              {commits.map((x) => (
                <li key={x.sha}>
                  <button className={"hist-commit" + (x.sha === sel ? " on" : "")} aria-current={x.sha === sel ? "true" : undefined} onClick={() => setSel(x.sha)}>
                    <span className="hist-subject">{x.subject}</span>
                    <span className="hist-meta">{x.author} · {fmtTime(x.time)}</span>
                    <span className="hist-counts">
                      {x.changes.added.length > 0 && <span className="hist-cnt create">+{x.changes.added.length}</span>}
                      {x.changes.changed.length > 0 && <span className="hist-cnt alter">≠{x.changes.changed.length}</span>}
                      {x.changes.dropped.length > 0 && <span className="hist-cnt drop">−{x.changes.dropped.length}</span>}
                      <code className="muted">{short(x.sha)}</code>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {hasMore && <div className="muted hist-hint" style={{ padding: 8 }}>{t("Only the latest 100 commits are listed.")}</div>}
          </nav>
          <div className="sch-detail">
            {selCommit && (
              <div className="hist-head">
                <div><b>{selCommit.subject}</b></div>
                <div className="muted">{t("{author} <{email}> · {time} · commit {sha}", { author: selCommit.author, email: selCommit.author_email, time: fmtTime(selCommit.time), sha: short(selCommit.sha) })}</div>
                <div className="hist-mig">
                  <label htmlFor="hist-from">{t("Migration script from")}</label>
                  <select id="hist-from" className="text" value={fromSha} onChange={(e) => setFromSha(e.target.value)}>
                    <option value="">{t("— choose a commit —")}</option>
                    {commits.filter((x) => x.sha !== sel).map((x) => <option key={x.sha} value={x.sha}>{short(x.sha)} · {fmtTime(x.time)} · {x.subject}</option>)}
                  </select>
                  <span>{t("to this commit")}</span>
                  <button className="btn" disabled={!fromSha || !!busy} onClick={migrate}>{busy === "migration" ? <IconSpinner /> : null}{t("Generate")}</button>
                </div>
              </div>
            )}
            {pane.kind === "migration" ? (
              <>
                <div className="toolbar">
                  <button className="btn" onClick={() => setPane({ kind: "diff" })}>← {t("Back to the changes")}</button>
                  <span className="muted">{t("{create} to create · {alter} to alter · {drop} to drop", { create: pane.result.summary.create ?? 0, alter: pane.result.summary.alter ?? 0, drop: pane.result.summary.drop ?? 0 })}</span>
                  <span className="grow" />
                  <button className="btn" onClick={() => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([pane.result.script], { type: "application/sql" })); a.download = `${safeName(dbName)}-${short(pane.from)}-${short(sel)}.sql`; a.click(); }}><IconDownload /> {t("Download")}</button>
                  <button className="btn" onClick={() => { navigator.clipboard?.writeText(pane.result.script); app.toast(t("Script copied"), "info"); }}><IconCopy /> {t("Copy")}</button>
                  <button className="btn" onClick={() => app.openQueryTool(sid, dbid, pane.result.script)}><IconTerminal /> {t("Open in Query Tool")}</button>
                </div>
                {pane.result.warnings.length > 0 && <div className="sch-warnings">{pane.result.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}</div>}
                <div className="sch-fill" aria-label={t("Migration script")}><SqlEditor value={pane.result.script} readOnly /></div>
              </>
            ) : (
              <div className="hist-body">
                <div className="hist-files" role="list" aria-label={t("Changed objects")}>
                  {objectFiles.map((f) => (
                    <div role="listitem" key={f.path}>
                      <button className={"hist-file" + (file?.path === f.path ? " on" : "")} aria-current={file?.path === f.path ? "true" : undefined}
                        onClick={() => { setFile(f); setPane({ kind: "diff" }); }} title={f.path}>
                        <span className={"hist-badge " + STATUS_CLASS[f.status]} aria-label={t(STATUS_LABEL[f.status])}>{STATUS_SIGN[f.status]}</span>
                        <span className="hist-fname">{f.object ? (f.object.name ? `${f.object.label} ${f.object.name}` : f.object.label) : f.path}
                          {f.object?.schema && <span className="muted"> · {f.object.schema}</span>}</span>
                      </button>
                    </div>
                  ))}
                </div>
                <div className="hist-view">
                  {file && detail && (
                    <div className="toolbar">
                      <span className="mono">{file.path}</span>
                      <span className="grow" />
                      {pane.kind === "file" && <button className="btn small" onClick={() => setPane({ kind: "diff" })}>{t("Show changes")}</button>}
                      {file.status !== "dropped" && <button className="btn small" onClick={() => show(detail.sha, file, false)}>{t("Show at this commit")}</button>}
                      {file.status !== "added" && detail.parent && <button className="btn small" onClick={() => show(detail.parent!, file, true)}>{t("Show before")}</button>}
                    </div>
                  )}
                  {pane.kind === "file" ? (
                    <>
                      <div className="hist-subtitle">{pane.title}</div>
                      <div className="sch-fill"><SqlEditor value={pane.content} readOnly /></div>
                    </>
                  ) : file ? <UnifiedDiff diff={file.diff} /> : <div className="empty-state">{t("Select an object")}</div>}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

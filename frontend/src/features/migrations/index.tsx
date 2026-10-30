/** Roadmap 2.2: versioned migrations - numbered SQL scripts from git applied in order, with a dry run first. */
import { useEffect, useState } from "react";
import { errMsg, useApp } from "../../context";
import type { AppApi } from "../../context";
import type { MenuItem } from "../../components/ContextMenu";
import type { Job, Selection } from "../../types";
import { N_, t } from "../../i18n";
import { registerMenu, registerTabKind } from "../../registry";
import { IconRefresh } from "../../components/Icons";
import { JobLog, Section } from "../maintenance/common";
import { fmtTime } from "../maintenance/api";
import "../maintenance/maintenance.css";
import "../monitoring/monitoring.css";
import "./migrations.css";
import { migApi, type DryRun, type Source, type State, type Status } from "./api";

const STATE_LABEL: Record<State, string> = {
  applied: "applied", pending: "pending", changed: "changed", failed: "failed", late: "late (out of order)",
  missing: "script missing", baseline: "below the baseline", baseline_record: "baseline", above_target: "above the target",
};
const STATE_CLASS: Record<State, string> = {
  applied: "finished", pending: "running", changed: "failed", failed: "failed", late: "failed", missing: "cancelled",
  baseline: "cancelled", baseline_record: "finished", above_target: "cancelled",
};

// the server's reasons why an apply needs a dry run first (translated when shown)
export const DRY_RUN_REASONS = [N_("Run a dry run first"), N_("The last dry run failed: fix the scripts and run the dry run again"),
  N_("The scripts or the database changed since the last dry run: run it again"), N_("The last dry run is older than 24 hours: run it again")];

const emptySource = (): Source => ({ kind: "git", url: "", ref: "main", path: "", token: null, out_of_order: false, copy_mode: "metadata", scratch: null });

function SourceForm(props: { sid: string; dbid: string; value: Source | null; onSaved: () => void }) {
  const app = useApp();
  const [f, setF] = useState<Source>(props.value ?? emptySource());
  const [busy, setBusy] = useState(false);
  useEffect(() => setF(props.value ?? emptySource()), [props.value]);
  const set = <K extends keyof Source>(k: K, v: Source[K]) => setF({ ...f, [k]: v });
  const git = f.kind === "git";
  return (
    <div className="form mform">
      <span className="l">{t("Scripts from")}</span>
      <div className="mig-radios" role="radiogroup" aria-label={t("Scripts from")}>
        <label className="check"><input type="radio" name="mig-kind" checked={git} onChange={() => set("kind", "git")} /> {t("a git repository (fetched by fbadmin)")}</label>
        <label className="check"><input type="radio" name="mig-kind" checked={!git} onChange={() => set("kind", "directory")} /> {t("a directory on the fbadmin host")}</label>
      </div>
      {git && <>
        <label className="l" htmlFor="mig-url">{t("Repository")}</label>
        <input id="mig-url" className="text mono" placeholder="https://github.com/acme/app.git" value={f.url ?? ""} onChange={(e) => set("url", e.target.value)} />
        <label className="l" htmlFor="mig-ref">{t("Branch, tag or commit")}</label>
        <input id="mig-ref" className="text mono" style={{ width: 220 }} value={f.ref} onChange={(e) => set("ref", e.target.value)} />
      </>}
      <label className="l" htmlFor="mig-path">{git ? t("Folder in the repository") : t("Directory")}</label>
      <input id="mig-path" className="text mono" placeholder={git ? "db/migrations" : "/srv/app/db/migrations"} value={f.path} onChange={(e) => set("path", e.target.value)} />
      {git && <>
        <label className="l" htmlFor="mig-token">{t("HTTPS token (optional)")}</label>
        <input id="mig-token" className="text mono" style={{ width: 220 }} placeholder="env:GIT_TOKEN" value={f.token ?? ""} onChange={(e) => set("token", e.target.value || null)} />
      </>}
      <label className="l" htmlFor="mig-copy">{t("Dry run on")}</label>
      <select id="mig-copy" className="text" value={f.copy_mode} onChange={(e) => set("copy_mode", e.target.value as Source["copy_mode"])}>
        <option value="metadata">{t("a metadata-only copy (fast)")}</option>
        <option value="full">{t("a full copy with the data (catches data errors)")}</option>
      </select>
      <span />
      <label className="check"><input type="checkbox" checked={f.out_of_order} onChange={(e) => set("out_of_order", e.target.checked)} /> {t("Also apply scripts with a lower version than applied ones (out of order)")}</label>
      <span />
      <div className="mt-row">
        <button className="btn primary" disabled={busy || (git ? !f.url?.trim() : !f.path.trim())} onClick={async () => {
          setBusy(true);
          try { await migApi.saveSource(props.sid, props.dbid, { ...f, url: f.url?.trim() || null, path: f.path.trim(), token: f.token?.trim() || null }); app.toast(t("Saved"), "ok"); props.onSaved(); }
          catch (e) { app.toast(errMsg(e), "error"); } finally { setBusy(false); }
        }}>{t("Save")}</button>
      </div>
    </div>
  );
}

function DryRunReport(props: { r: DryRun }) {
  const r = props.r;
  return (
    <Section wide title={<>{t("Dry run")} <span className={"pill " + (r.ok ? "finished" : "failed")}>{r.ok ? t("passed") : t("failed")}</span></>}
      hint={t("{time} on a {copy} copy, {n} s; the copy was dropped", { time: fmtTime(r.finished_at), copy: r.copy === "full" ? t("full") : t("metadata-only"), n: r.seconds })}>
      <ul className="mig-results">{r.scripts.map((s) => (
        <li key={s.version} className={s.ok ? "ok" : "err"}>
          <span aria-hidden="true">{s.ok ? "✓" : "✕"}</span> <span className="mono">{s.script}</span>{" "}
          {s.ok ? t("{n} statement(s), {ms} ms", { n: s.statements, ms: s.ms }) : t("line {line}: {message}", { line: s.error!.line, message: s.error!.message })}
          {s.error && <pre className="code-block" tabIndex={0} aria-label={t("Failed statement of {script}", { script: s.script })}>{s.error.sql}</pre>}
        </li>
      ))}</ul>
      {r.changes.length > 0 && <>
        <h3>{t("Schema changes")}</h3>
        <p className="muted">{t("{created} created, {altered} altered, {dropped} dropped", { created: r.summary.created ?? 0, altered: r.summary.altered ?? 0, dropped: r.summary.dropped ?? 0 })}</p>
        <ul className="mig-changes">{r.changes.map((c, i) => (
          <li key={i}>
            <details>
              <summary><span className={"mig-chg " + c.change}>{t(c.change)}</span> {c.type} <span className="mono">{c.schema ? `${c.schema}.` : ""}{c.name}</span></summary>
              {c.before && <><div className="muted">{t("Before")}</div><pre className="code-block">{c.before}</pre></>}
              {c.after && <><div className="muted">{t("After")}</div><pre className="code-block">{c.after}</pre></>}
            </details>
          </li>
        ))}</ul>
      </>}
    </Section>
  );
}

function MigrationsTab(props: { serverId: string; dbId: string; visible: boolean }) {
  const app = useApp();
  const { serverId: sid, dbId: dbid } = props;
  const [src, setSrc] = useState<Source | null | undefined>(undefined);
  const [st, setSt] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dry, setDry] = useState<DryRun | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [editing, setEditing] = useState(false);
  const [base, setBase] = useState("");
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    try {
      const s = await migApi.source(sid, dbid);
      setSrc(s);
      if (s) { setSt(await migApi.status(sid, dbid)); setError(null); }
      setDry(await migApi.lastDryRun(sid, dbid).catch(() => null));
    } catch (e) { setError(errMsg(e)); setSt(null); } finally { setLoading(false); }
  };
  useEffect(() => { if (props.visible) load(); }, [props.visible, sid, dbid]);
  const run = async (fn: () => Promise<Job>) => {
    try { setRunning(true); setJob(await fn()); } catch (e) { setRunning(false); app.toast(errMsg(e), "error"); }
  };
  const pending = st?.pending.length ?? 0;
  const blocked = (st?.errors.length ?? 0) > 0;
  const canApply = !!st && pending > 0 && !blocked && !st.dry_run_needed && !running;
  const hasHistory = !!st?.items.some((i) => i.state === "applied" || i.state === "failed" || i.state === "baseline_record" || i.state === "missing");
  return (
    <div className="mt-body mig">
      <Section wide title={t("Migration scripts")}
        hint={t("Numbered scripts V<version>__<description>.sql, applied in version order. The database records each applied script, with its checksum, in FBADMIN_MIGRATIONS.")}
        right={<>{src && <button className="btn small" onClick={() => setEditing(!editing)} aria-expanded={editing}>{t("Source…")}</button>}<button className="btn small" onClick={load} aria-label={t("Refresh")} disabled={loading}><IconRefresh size={12} /></button></>}>
        {src === null || editing ? <SourceForm sid={sid} dbid={dbid} value={src ?? null} onSaved={() => { setEditing(false); load(); }} /> : null}
        {error && <div className="err">{error}</div>}
        {st && <>
          <div className="cards">
            <div className="card"><div className="k">{t("Database version")}</div><div className="v">{st.current ?? "—"}</div></div>
            <div className="card"><div className="k">{t("Pending")}</div><div className={"v" + (pending ? " warn" : "")}>{pending}</div></div>
            <div className="card"><div className="k">{t("Scripts")}</div><div className="v">{st.scripts}</div></div>
            <div className="card"><div className="k">{t("Source")}</div><div className="v mono mig-ref">{st.source_ref ?? "—"}</div></div>
          </div>
          {(st.errors.length > 0 || st.warnings.length > 0) && <div className="alerts">
            {st.errors.map((e, i) => <div key={"e" + i} className="alert critical"><span className="lvl">{t("error")}</span><span>{e}</span></div>)}
            {st.warnings.map((w, i) => <div key={"w" + i} className="alert warning"><span className="lvl">{t("warning")}</span><span>{w}</span></div>)}
          </div>}
          <div className="grid-wrap" tabIndex={0} role="region" aria-label={t("Migration scripts")} style={{ maxHeight: 360 }}><table className="grid">
            <thead><tr><th>{t("Version")}</th><th>{t("Description")}</th><th>{t("Script")}</th><th>{t("State")}</th><th>{t("Applied")}</th><th>{t("By")}</th><th>{t("Time")}</th></tr></thead>
            <tbody>{st.items.map((i) => (
              <tr key={i.version + (i.script ?? "")}>
                <td className="num mono">{i.version}</td><td>{i.description}</td><td className="mono">{i.script ?? "—"}</td>
                <td><span className={"pill " + STATE_CLASS[i.state]}>{t(STATE_LABEL[i.state])}</span>{i.error && <div className="err mig-err">{i.error}</div>}</td>
                <td>{i.installed_at ? fmtTime(i.installed_at) : "—"}</td><td>{i.installed_by ?? ""}</td>
                <td className="num">{i.execution_ms != null ? `${i.execution_ms} ms` : ""}</td>
              </tr>
            ))}</tbody>
          </table></div>
          <div className="mt-row mig-actions">
            <button className="btn" disabled={!pending || blocked || running} onClick={() => run(() => migApi.dryRun(sid, dbid))}>{t("Dry run")}</button>
            <button className="btn primary" disabled={!canApply} onClick={() => {
              if (confirm(t("Apply {n} script(s) to {db}?", { n: pending, db: app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? "" }))) run(() => migApi.apply(sid, dbid));
            }}>{t("Apply {n} pending", { n: pending })}</button>
            {pending > 0 && !blocked && st.dry_run_needed && <span className="muted">{t(st.dry_run_needed)}</span>}
            {blocked && <button className="btn" onClick={async () => {
              if (!confirm(t("Repair: remove the records of failed scripts (after you fixed the database by hand) and accept the checksums of changed scripts?"))) return;
              try { const r = await migApi.repair(sid, dbid); app.toast(t("Removed {failed} failed, accepted {changed} checksum(s)", { failed: r.removed_failed.length, changed: r.accepted_checksums.length }), "ok"); load(); }
              catch (e) { app.toast(errMsg(e), "error"); }
            }}>{t("Repair…")}</button>}
            {pending > 0 && !blocked && <button className="btn danger" disabled={running} onClick={() => {
              if (confirm(t("Apply without a dry run? A script that fails half way leaves the database partly changed."))) run(() => migApi.apply(sid, dbid, true));
            }}>{t("Apply without a dry run…")}</button>}
          </div>
          {!hasHistory && <div className="mt-row mig-baseline">
            <label htmlFor="mig-base" className="muted">{t("Existing database: it is at version")}</label>
            <input id="mig-base" className="text mono" style={{ width: 100 }} value={base} onChange={(e) => setBase(e.target.value)} placeholder="3" />
            <button className="btn small" disabled={!base.trim()} onClick={async () => {
              try { await migApi.baseline(sid, dbid, base.trim(), "Baseline"); setBase(""); load(); } catch (e) { app.toast(errMsg(e), "error"); }
            }}>{t("Set the baseline")}</button>
          </div>}
        </>}
        <JobLog job={job} onDone={() => { setRunning(false); load(); }} />
      </Section>
      {dry && <DryRunReport r={dry} />}
    </div>
  );
}

export function openMigrations(app: AppApi, sid: string, dbid: string) {
  const db = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid)?.name ?? t("database");
  app.openTab({ kind: "migrations", title: t("{db} · migrations", { db }), serverId: sid, dbId: dbid });
}

registerTabKind("migrations", {
  icon: <span style={{ fontSize: 12 }} aria-hidden="true">⇡</span>,
  render: (tab, visible) => <MigrationsTab serverId={tab.serverId!} dbId={tab.dbId!} visible={visible} />,
});

registerMenu((app: AppApi, sel: Selection): MenuItem[] => {
  if (sel.kind !== "database" || !sel.dbId) return [];
  const sid = sel.serverId, dbid = sel.dbId;
  return [{ label: t("Migrations…"), onClick: () => openMigrations(app, sid, dbid) }];
});

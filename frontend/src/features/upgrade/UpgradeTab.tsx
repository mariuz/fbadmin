import { useEffect, useMemo, useState } from "react";
import { request } from "../../api";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import type { Job } from "../../types";
import { IconDownload, IconPlay, IconSpinner } from "../../components/Icons";
import { JobLog } from "../maintenance/common";
import { saveBlob } from "../schema/api";
import { N_, t } from "../../i18n";
import { upgradeApi, type Finding, type Severity, type UpgradeObject, type UpgradeReport } from "./api";

const SEVERITIES: Severity[] = ["blocker", "warning", "info"];
const SEV_LABEL: Record<Severity, string> = { blocker: N_("Blockers"), warning: N_("Warnings"), info: N_("Info") };
const SEV_ONE: Record<Severity, string> = { blocker: N_("Blocker"), warning: N_("Warning"), info: N_("Info") };
// The categories the backend uses (marked here so they get translated).
N_("Reserved words"); N_("Removed or changed features"); N_("Data types and SQL behaviour");
N_("Security and authentication"); N_("Server configuration"); N_("Storage and ODS"); N_("SQL schemas (Firebird 6)");
const ALL_VERSIONS = ["4.0", "5.0", "6.0"];

const majorCache: Record<string, Promise<number>> = {};
function engineMajor(sid: string): Promise<number> {
  majorCache[sid] = majorCache[sid] ?? request<{ engine_major: number }>("GET", `/api/servers/${sid}/maintenance/capabilities`)
    .then((c) => c.engine_major).catch((e) => { delete majorCache[sid]; throw e; });
  return majorCache[sid];
}

export function UpgradeTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const [srcMajor, setSrcMajor] = useState<number | null>(null);
  const [majors, setMajors] = useState<Record<string, number>>({});
  const [target, setTarget] = useState("5.0");
  const [targetServer, setTargetServer] = useState("");
  const [scratch, setScratch] = useState("");
  const [metadataOnly, setMetadataOnly] = useState(false);
  const [keep, setKeep] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<UpgradeReport | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [shown, setShown] = useState<Record<Severity, boolean>>({ blocker: true, warning: true, info: true });

  useEffect(() => {
    engineMajor(sid).then((m) => {
      setSrcMajor(m);
      setTarget(m >= 5 ? "6.0" : "5.0");
    }).catch((e) => setError(errMsg(e)));
    for (const s of app.servers) {
      if (s.id === sid) continue;
      engineMajor(s.id).then((m) => setMajors((x) => ({ ...x, [s.id]: m }))).catch(() => undefined);
    }
  }, [sid]);

  const targets = ALL_VERSIONS.filter((v) => srcMajor === null || Number(v) > srcMajor);
  const candidates = app.servers.filter((s) => s.id !== sid && srcMajor !== null && (majors[s.id] ?? 0) > srcMajor);
  const pickServer = (id: string) => {
    setTargetServer(id);
    if (id && majors[id]) setTarget(`${majors[id]}.0`);
  };
  const defaultScratch = useMemo(() => {
    const srv = app.servers.find((s) => s.id === targetServer);
    const src = app.servers.find((s) => s.id === sid)?.databases.find((d) => d.id === dbid);
    const p = srv?.databases[0]?.path ?? src?.path ?? "";
    const m = p.match(/^(.*[\\/])[^\\/]+$/);
    return m ? `${m[1]}fbadmin_upgrade_….fdb` : "";
  }, [targetServer, app.servers]);

  const run = async () => {
    setError(null);
    setBusy(true);
    try {
      if (targetServer) {
        setReport(null);
        setJob(await upgradeApi.trial(sid, dbid, {
          target_server_id: targetServer, target, scratch_path: scratch.trim() || null, metadata_only: metadataOnly, keep,
        }));
      } else {
        setJob(null);
        setReport(await upgradeApi.check(sid, dbid, target));
        setBusy(false);
      }
    } catch (e) {
      setError(errMsg(e));
      setBusy(false);
    }
  };
  const jobDone = async (j: Job) => {
    try {
      const r = await upgradeApi.trialResult(j.id);
      if (r.report) setReport(r.report);
      else if (r.error || j.error) setError(r.error ?? j.error);
      if (r.report?.trial?.registered_as) app.reloadServers();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const download = async (format: "md" | "html") => {
    if (!report) return;
    try {
      const f = await upgradeApi.render(report, format);
      saveBlob(new Blob([f.content], { type: f.media_type }), f.filename);
    } catch (e) {
      setError(errMsg(e));
    }
  };

  return (
    <div className="sch-col sch-scroll">
      <div className="upg">
        <section aria-labelledby="upg-setup">
          <h2 id="upg-setup">{t("Upgrade assistant")}</h2>
          <p className="muted">{t("Checks this database before it moves to a newer Firebird version: reserved words, removed or changed features, the ODS change and the backup/restore plan. With a target server, it also backs the database up here, restores it there into a scratch database and recompiles every view and PSQL module with the new server.")}</p>
          <div className="sch-row">
            <label>{t("Target version")}
              <select className="text" value={target} disabled={!!targetServer} onChange={(e) => setTarget(e.target.value)}>
                {targets.map((v) => <option key={v} value={v}>Firebird {v}</option>)}
              </select>
            </label>
            <label>{t("Trial restore on")}
              <select className="text" value={targetServer} onChange={(e) => pickServer(e.target.value)}>
                <option value="">{t("No trial restore (checks only)")}</option>
                {candidates.map((s) => <option key={s.id} value={s.id}>{s.name} (Firebird {majors[s.id]})</option>)}
              </select>
            </label>
            {srcMajor !== null && <span className="muted">{t("Source: Firebird {v}", { v: srcMajor })}</span>}
          </div>
          {targetServer && (
            <div className="sch-row">
              <label>{t("Trial database path on the target server")}
                <input className="text upg-path" value={scratch} placeholder={defaultScratch} onChange={(e) => setScratch(e.target.value)} />
              </label>
              <label><input type="checkbox" checked={metadataOnly} onChange={(e) => setMetadataOnly(e.target.checked)} /> {t("Metadata only (faster)")}</label>
              <label><input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} /> {t("Keep and register the restored database")}</label>
            </div>
          )}
          <div className="sch-row">
            <button className="btn primary" onClick={run} disabled={busy || !targets.length}>
              {busy ? <IconSpinner /> : <IconPlay />} {targetServer ? t("Run checks and trial restore") : t("Run checks")}
            </button>
            {report && <>
              <button className="btn" onClick={() => download("md")}><IconDownload /> {t("Download Markdown")}</button>
              <button className="btn" onClick={() => download("html")}><IconDownload /> {t("Download HTML")}</button>
            </>}
            {srcMajor !== null && !targets.length && <span className="muted">{t("This database already runs on the newest supported version.")}</span>}
          </div>
          {error && <div className="err" role="alert">{error}</div>}
          {job && <JobLog job={job} onDone={jobDone} maxHeight={180} />}
        </section>
        {report && <Report report={report} shown={shown} setShown={setShown} sid={sid} dbid={dbid} />}
      </div>
    </div>
  );
}

function Report(props: { report: UpgradeReport; shown: Record<Severity, boolean>; setShown: (s: Record<Severity, boolean>) => void; sid: string; dbid: string }) {
  const { report, shown } = props;
  const src = report.source;
  const groups = useMemo(() => {
    const out: Array<[string, Finding[]]> = [];
    for (const f of report.findings) {
      if (!shown[f.severity]) continue;
      const g = out.find(([c]) => c === f.category);
      if (g) g[1].push(f);
      else out.push([f.category, [f]]);
    }
    return out;
  }, [report, shown]);
  return (
    <>
      <section aria-labelledby="upg-summary">
        <h2 id="upg-summary">{t("{db}: Firebird {from} → {to}", { db: report.database.name, from: src.engine_version, to: report.target.version })}</h2>
        <table className="upg-facts">
          <thead><tr><th scope="col"></th><th scope="col">{t("Source")}</th><th scope="col">{t("Target")}</th></tr></thead>
          <tbody>
            <tr><th scope="row">{t("Version")}</th><td>{src.engine_version}</td><td>{report.trial?.target_engine ?? report.target.version}</td></tr>
            <tr><th scope="row">ODS</th><td>{src.ods}</td><td>{report.trial?.restored?.ods ?? report.target.ods}</td></tr>
            <tr><th scope="row">{t("Page size")}</th><td>{src.page_size}</td><td>{report.trial?.restored?.page_size ?? ""}</td></tr>
            <tr><th scope="row">{t("Dialect")}</th><td>{src.dialect}</td><td></td></tr>
          </tbody>
        </table>
        <div className="sch-row" role="group" aria-label={t("Show findings by severity")}>
          {SEVERITIES.map((s) => (
            <label key={s} className={"upg-count " + s}>
              <input type="checkbox" checked={shown[s]} onChange={(e) => props.setShown({ ...shown, [s]: e.target.checked })} />
              {t(SEV_LABEL[s])}: <b>{report.counts[s] ?? 0}</b>
            </label>
          ))}
          {report.trial?.registered_as && <span className="muted">{t("Restored copy registered as {name}", { name: report.trial.registered_as })}</span>}
        </div>
      </section>
      <section aria-labelledby="upg-findings">
        <h2 id="upg-findings">{t("Findings")}</h2>
        {!groups.length && <p className="muted">{t("Nothing to show.")}</p>}
        {groups.map(([cat, fs]) => (
          <div key={cat} className="upg-cat">
            <h3>{t(cat)} <span className="muted">({fs.length})</span></h3>
            {fs.map((f) => <FindingView key={f.id} f={f} sid={props.sid} dbid={props.dbid} />)}
          </div>
        ))}
      </section>
      <section aria-labelledby="upg-plan">
        <h2 id="upg-plan">{t("Migration plan")}</h2>
        <ol className="upg-plan">
          {report.plan.map((s, i) => (
            <li key={i}>
              <h3>{s.title}</h3>
              {s.text.map((p, j) => <p key={j}>{p}</p>)}
              {s.table && (
                <div className="upg-tablewrap">
                  <table className="sch-map">
                    <thead><tr>{s.table.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
                    <tbody>{s.table.rows.map((r, k) => <tr key={k}>{r.map((c, l) => <td key={l}>{c}</td>)}</tr>)}</tbody>
                  </table>
                </div>
              )}
              {s.commands.length > 0 && <pre className="upg-cmd">{s.commands.join("\n")}</pre>}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}

function FindingView({ f, sid, dbid }: { f: Finding; sid: string; dbid: string }) {
  const app = useApp();
  const [open, setOpen] = useState(f.severity === "blocker");
  const select = (o: UpgradeObject) => o.link && app.select({ kind: "object", serverId: sid, dbId: dbid, otype: o.link.otype, name: o.link.name });
  return (
    <details className={"upg-finding " + f.severity} open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>
        <span className={"upg-sev " + f.severity}>{t(SEV_ONE[f.severity])}</span> {f.title}
        {f.objects.length > 0 && <span className="muted"> · {t("{n} object(s)", { n: f.objects.length })}</span>}
      </summary>
      <p>{f.explanation}</p>
      <p><b>{t("Fix")}:</b> {f.fix}</p>
      {f.objects.length > 0 && (
        <ul className="upg-objects">
          {f.objects.slice(0, 500).map((o, i) => (
            <li key={i}>
              <span className="muted">{o.type}</span>{" "}
              {o.link
                ? <a href="#" onClick={(e) => { e.preventDefault(); select(o); }}>{o.parent && o.type !== o.link.otype.replace(/s$/, "") ? `${o.parent}.${o.name}` : o.name}</a>
                : <code>{o.name}</code>}
              {o.detail && <span className="muted"> — {o.detail}</span>}
              {o.fix && <div className="upg-fix"><code>{o.fix}</code></div>}
            </li>
          ))}
          {f.objects.length > 500 && <li className="muted">{t("… and {n} more", { n: f.objects.length - 500 })}</li>}
        </ul>
      )}
      {f.refs.filter(Boolean).map((r) => <p key={r} className="muted">{t("See")} <a href={r} target="_blank" rel="noreferrer">{r}</a></p>)}
    </details>
  );
}

import { useMemo, useState } from "react";
import type { Job } from "../../types";
import { fmtBytes, mapi, waitJob, type GstatResult, type GstatTable } from "./api";
import { BarList, FillHistogram } from "./charts";
import { Check, JobLog, Section, useBusy } from "./common";

const SEV_ICON = { critical: "⛔", warning: "⚠", info: "ℹ" } as const;
type Metric = "size" | "records" | "versions" | "fill";

export function GstatSection(props: { serverId: string; dbId: string }) {
  const [res, setRes] = useState<GstatResult | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [system, setSystem] = useState(false);
  const [paste, setPaste] = useState("");
  const [showPaste, setShowPaste] = useState(false);
  const { busy, error, run } = useBusy();

  const start = () => run(async () => {
    setRes(null);
    const j = await mapi.gstat(props.serverId, props.dbId, { system, record_versions: true });
    setJob(j);
    const done = await waitJob(j.id);
    if (done.status === "finished") setRes(await mapi.gstatResult(props.serverId, j.id));
  });
  return (
    <>
      <Section title="Database statistics (gstat)" hint="Data pages, fill distribution, record versions, fragments and index structure — read from the database file by the server's gstat via the Services API.">
        <div className="mt-row">
          <button className="btn primary" disabled={busy} onClick={start}>{busy ? "Analyzing…" : "Analyze database"}</button>
          <Check label="Include system tables" checked={system} onChange={setSystem} />
          <span style={{ flex: 1 }} />
          <button className="btn small" onClick={() => setShowPaste(!showPaste)}>Parse pasted gstat output…</button>
        </div>
        {showPaste && (
          <div style={{ marginTop: 8 }}>
            <textarea className="text mono" rows={6} style={{ width: "100%" }} placeholder="Paste the output of gstat -a -r here" value={paste} onChange={(e) => setPaste(e.target.value)} />
            <button className="btn" disabled={!paste.trim() || busy} onClick={() => run(async () => setRes(await mapi.gstatParse(props.serverId, paste)))}>Parse</button>
          </div>
        )}
        {error && <div className="err" style={{ marginTop: 8 }}>{error}</div>}
        {job && !res && <JobLog job={job} maxHeight={120} />}
      </Section>
      {res && <GstatReport res={res} />}
    </>
  );
}

export function GstatReport(props: { res: GstatResult }) {
  const { res } = props;
  const [metric, setMetric] = useState<Metric>("size");
  const [pick, setPick] = useState<string | null>(null);
  const tables = res.tables;
  const totals = useMemo(() => ({
    size: tables.reduce((a, t) => a + (t.table_size ?? 0), 0),
    records: tables.reduce((a, t) => a + (t.total_records ?? 0), 0),
    versions: tables.reduce((a, t) => a + (t.total_versions ?? 0), 0),
    indices: tables.reduce((a, t) => a + t.indices.length, 0),
    maxDepth: Math.max(0, ...tables.flatMap((t) => t.indices.map((i) => i.depth ?? 0))),
  }), [tables]);
  const flagged = new Set(res.attention.map((f) => f.table));
  const series = useMemo(() => {
    const val = (t: GstatTable) => metric === "size" ? t.table_size ?? 0 : metric === "records" ? t.total_records ?? 0
      : metric === "versions" ? t.total_versions ?? 0 : t.average_fill ?? 0;
    return [...tables].filter((t) => metric !== "fill" || (t.data_pages ?? 0) > 0).sort((a, b) => val(b) - val(a)).slice(0, 15).map((t) => ({
      label: t.name, value: val(t), flag: flagged.has(t.name),
      display: metric === "size" ? fmtBytes(val(t)) : metric === "fill" ? `${val(t)}%` : val(t).toLocaleString(),
      title: `${t.name}: ${(t.total_records ?? 0).toLocaleString()} records, ${(t.total_versions ?? 0).toLocaleString()} back versions, ${t.data_pages ?? 0} data pages, ${t.average_fill ?? 0}% fill`,
    }));
  }, [tables, metric]);
  const indexSeries = useMemo(() => tables.flatMap((t) => t.indices.map((i) => ({ t, i })))
    .sort((a, b) => (b.i.depth ?? 0) - (a.i.depth ?? 0) || (b.i.nodes ?? 0) - (a.i.nodes ?? 0)).slice(0, 15)
    .map(({ t, i }) => ({ label: i.name, value: i.depth ?? 0, flag: (i.depth ?? 0) >= 4, display: `depth ${i.depth ?? 0}`,
      title: `${i.name} on ${t.name}: depth ${i.depth}, ${i.leaf_buckets} leaf buckets, ${i.nodes} nodes, avg key ${i.average_key_length}, max dup ${i.max_dup}` })), [tables]);
  const selected = pick ? tables.find((t) => t.name === pick) : null;
  const h = res.header;
  return (
    <>
      <div className="gs-cards">
        <div className="card"><div className="v">{tables.length}</div><div className="k">tables</div></div>
        <div className="card"><div className="v">{fmtBytes(totals.size)}</div><div className="k">table data</div></div>
        <div className="card"><div className="v">{totals.records.toLocaleString()}</div><div className="k">records</div></div>
        <div className="card"><div className="v">{totals.versions.toLocaleString()}</div><div className="k">back versions</div></div>
        <div className="card"><div className="v">{totals.indices}</div><div className="k">indices · max depth {totals.maxDepth}</div></div>
        <div className="card"><div className="v">{h.page_size ?? "—"}</div><div className="k">page size · ODS {h.ods_version ?? "?"}</div></div>
        <div className="card"><div className="v">{typeof h.next_transaction === "number" && typeof h.oldest_active === "number" ? (h.next_transaction - h.oldest_active).toLocaleString() : "—"}</div><div className="k">next − oldest active</div></div>
      </div>
      <Section title={`Tables needing attention (${res.attention.length})`}>
        {res.attention.length === 0 ? <div className="ok">✓ Nothing stands out: no excessive record versions, fragmentation, low fill or deep indices.</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>Severity</th><th>Table</th><th>Index</th><th>Issue</th><th>Detail</th><th>Advice</th></tr></thead>
            <tbody>{res.attention.map((f, i) => (
              <tr key={i} onClick={() => f.table && setPick(f.table)} style={{ cursor: f.table ? "pointer" : undefined }}>
                <td><span className={"sev " + f.severity}>{SEV_ICON[f.severity]} {f.severity}</span></td>
                <td>{f.table ?? "—"}</td><td>{f.index ?? "—"}</td><td>{f.issue}</td><td>{f.detail}</td>
                <td style={{ whiteSpace: "normal", minWidth: 260 }}>{f.advice}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Section>
      <Section title="Tables" right={
        <div className="seg">
          {([["size", "Size"], ["records", "Records"], ["versions", "Back versions"], ["fill", "Avg fill %"]] as const).map(([k, l]) => (
            <button key={k} className={metric === k ? "on" : ""} onClick={() => setMetric(k)}>{l}</button>
          ))}
        </div>
      } hint="Top 15 tables; darker bars are tables listed under “needs attention”. Click a bar for details.">
        <BarList data={series} max={metric === "fill" ? 100 : undefined} onPick={setPick} empty="No user tables." />
      </Section>
      <Section title="Index depth" hint="Indices deeper than 3 levels cost extra page reads per lookup; a larger page size reduces depth.">
        <BarList data={indexSeries} max={Math.max(4, totals.maxDepth)} empty="No indices." />
      </Section>
      {selected && <TableDetail t={selected} onClose={() => setPick(null)} />}
      <Section title="All tables">
        <div className="grid-wrap" style={{ maxHeight: 520 }}><table className="grid">
          <thead><tr><th>Table</th><th>Records</th><th>Versions</th><th>Max ver.</th><th>Fragments</th><th>Data pages</th><th>Avg fill</th><th>Fill distribution</th><th>Size</th><th>Indices</th></tr></thead>
          <tbody>{tables.map((t) => (
            <tr key={t.name} className={pick === t.name ? "sel" : ""} onClick={() => setPick(t.name)} style={{ cursor: "pointer" }}>
              <td>{flagged.has(t.name) ? "⚠ " : ""}{t.name}</td>
              <td className="num">{(t.total_records ?? 0).toLocaleString()}</td>
              <td className="num">{(t.total_versions ?? 0).toLocaleString()}</td>
              <td className="num">{t.max_versions ?? 0}</td>
              <td className="num">{(t.total_fragments ?? 0).toLocaleString()}</td>
              <td className="num">{t.data_pages ?? 0}</td>
              <td className="num">{t.average_fill ?? 0}%</td>
              <td><FillHistogram counts={t.fill_distribution} width={110} height={18} /></td>
              <td className="num">{fmtBytes(t.table_size)}{t.table_size_estimated ? "*" : ""}</td>
              <td className="num">{t.indices.length}</td>
            </tr>
          ))}</tbody>
        </table></div>
        <div className="hint" style={{ marginTop: 6 }}>* estimated from data and pointer pages (Firebird 3/4 do not report the table size).</div>
      </Section>
      <Section title="Raw output"><details><summary>Show gstat output</summary><pre className="log" style={{ maxHeight: 400 }}>{res.text}</pre></details></Section>
    </>
  );
}

function TableDetail(props: { t: GstatTable; onClose: () => void }) {
  const t = props.t;
  return (
    <Section title={<>Table {t.name}</>} right={<button className="btn small" onClick={props.onClose}>Close</button>}>
      <div className="mt-row" style={{ alignItems: "flex-start", gap: 24 }}>
        <table className="kv" style={{ width: 360 }}><tbody>
          {[["Records", t.total_records], ["Back versions", t.total_versions], ["Max versions", t.max_versions],
            ["Avg record length", t.average_record_length], ["Avg version length", t.average_version_length],
            ["Fragments", t.total_fragments], ["Compression ratio", t.compression_ratio], ["Data pages", t.data_pages],
            ["Primary / secondary pages", `${t.primary_pages ?? "—"} / ${t.secondary_pages ?? "—"}`], ["Empty / full pages", `${t.empty_pages ?? "—"} / ${t.full_pages ?? "—"}`],
            ["Blobs", t.blobs ?? 0], ["Formats", `${t.used_formats ?? "—"} used of ${t.total_formats ?? "—"}`]].map(([k, v]) => (
            <tr key={String(k)}><td>{k}</td><td>{typeof v === "number" ? v.toLocaleString() : String(v ?? "—")}</td></tr>
          ))}
        </tbody></table>
        <div>
          <div className="muted" style={{ marginBottom: 4 }}>Data page fill distribution</div>
          <FillHistogram counts={t.fill_distribution} width={240} height={70} />
        </div>
      </div>
      {t.indices.length > 0 && <>
        <h3>Indices</h3>
        <div className="grid-wrap"><table className="grid">
          <thead><tr><th>Index</th><th>Depth</th><th>Leaf buckets</th><th>Nodes</th><th>Avg key</th><th>Total dup</th><th>Max dup</th><th>Clustering</th><th>Fill distribution</th></tr></thead>
          <tbody>{t.indices.map((i) => (
            <tr key={i.name}>
              <td>{(i.depth ?? 0) >= 4 ? "⚠ " : ""}{i.name}</td><td className="num">{i.depth}</td><td className="num">{i.leaf_buckets}</td>
              <td className="num">{(i.nodes ?? 0).toLocaleString()}</td><td className="num">{i.average_key_length}</td>
              <td className="num">{(i.total_dup ?? 0).toLocaleString()}</td><td className="num">{(i.max_dup ?? 0).toLocaleString()}</td>
              <td className="num">{i.ratio ?? "—"}</td><td><FillHistogram counts={i.fill_distribution} width={110} height={18} /></td>
            </tr>
          ))}</tbody>
        </table></div>
      </>}
    </Section>
  );
}

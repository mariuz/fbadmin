import { useMemo, useState } from "react";
import type { Job } from "../../types";
import { t } from "../../i18n";
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
      <Section title={t("Database statistics (gstat)")} hint={t("Data pages, fill distribution, record versions, fragments and index structure — read from the database file by the server's gstat via the Services API.")}>
        <div className="mt-row">
          <button className="btn primary" disabled={busy} onClick={start}>{busy ? t("Analyzing…") : t("Analyze database")}</button>
          <Check label={t("Include system tables")} checked={system} onChange={setSystem} />
          <span style={{ flex: 1 }} />
          <button className="btn small" onClick={() => setShowPaste(!showPaste)}>{t("Parse pasted gstat output…")}</button>
        </div>
        {showPaste && (
          <div style={{ marginTop: 8 }}>
            <textarea className="text mono" rows={6} style={{ width: "100%" }} placeholder={t("Paste the output of gstat -a -r here")} value={paste} onChange={(e) => setPaste(e.target.value)} />
            <button className="btn" disabled={!paste.trim() || busy} onClick={() => run(async () => setRes(await mapi.gstatParse(props.serverId, paste)))}>{t("Parse")}</button>
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
    return [...tables].filter((t) => metric !== "fill" || (t.data_pages ?? 0) > 0).sort((a, b) => val(b) - val(a)).slice(0, 15).map((tb) => ({
      label: tb.name, value: val(tb), flag: flagged.has(tb.name),
      display: metric === "size" ? fmtBytes(val(tb)) : metric === "fill" ? `${val(tb)}%` : val(tb).toLocaleString(),
      title: t("{table}: {records} records, {versions} back versions, {pages} data pages, {fill}% fill", {
        table: tb.name, records: (tb.total_records ?? 0).toLocaleString(), versions: (tb.total_versions ?? 0).toLocaleString(),
        pages: tb.data_pages ?? 0, fill: tb.average_fill ?? 0,
      }),
    }));
  }, [tables, metric]);
  const indexSeries = useMemo(() => tables.flatMap((tb) => tb.indices.map((i) => ({ tb, i })))
    .sort((a, b) => (b.i.depth ?? 0) - (a.i.depth ?? 0) || (b.i.nodes ?? 0) - (a.i.nodes ?? 0)).slice(0, 15)
    .map(({ tb, i }) => ({ label: i.name, value: i.depth ?? 0, flag: (i.depth ?? 0) >= 4, display: t("depth {depth}", { depth: i.depth ?? 0 }),
      title: t("{index} on {table}: depth {depth}, {leaf} leaf buckets, {nodes} nodes, avg key {key}, max dup {dup}", {
        index: i.name, table: tb.name, depth: String(i.depth), leaf: String(i.leaf_buckets), nodes: String(i.nodes),
        key: String(i.average_key_length), dup: String(i.max_dup),
      }) })), [tables]);
  const selected = pick ? tables.find((t) => t.name === pick) : null;
  const h = res.header;
  return (
    <>
      <div className="gs-cards">
        <div className="card"><div className="v">{tables.length}</div><div className="k">{t("tables")}</div></div>
        <div className="card"><div className="v">{fmtBytes(totals.size)}</div><div className="k">{t("table data")}</div></div>
        <div className="card"><div className="v">{totals.records.toLocaleString()}</div><div className="k">{t("records")}</div></div>
        <div className="card"><div className="v">{totals.versions.toLocaleString()}</div><div className="k">{t("back versions")}</div></div>
        <div className="card"><div className="v">{totals.indices}</div><div className="k">{t("indices · max depth {depth}", { depth: totals.maxDepth })}</div></div>
        <div className="card"><div className="v">{h.page_size ?? "—"}</div><div className="k">{t("page size · ODS {ods}", { ods: h.ods_version ?? "?" })}</div></div>
        <div className="card"><div className="v">{typeof h.next_transaction === "number" && typeof h.oldest_active === "number" ? (h.next_transaction - h.oldest_active).toLocaleString() : "—"}</div><div className="k">{t("next − oldest active")}</div></div>
      </div>
      <Section title={t("Tables needing attention ({count})", { count: res.attention.length })}>
        {res.attention.length === 0 ? <div className="ok">✓ {t("Nothing stands out: no excessive record versions, fragmentation, low fill or deep indices.")}</div> : (
          <div className="grid-wrap"><table className="grid">
            <thead><tr><th>{t("Severity")}</th><th>{t("Table")}</th><th>{t("Index")}</th><th>{t("Issue")}</th><th>{t("Detail")}</th><th>{t("Advice")}</th></tr></thead>
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
      <Section title={t("Tables")} right={
        <div className="seg">
          {([["size", t("Size")], ["records", t("Records")], ["versions", t("Back versions")], ["fill", t("Avg fill %")]] as const).map(([k, l]) => (
            <button key={k} className={metric === k ? "on" : ""} onClick={() => setMetric(k)}>{l}</button>
          ))}
        </div>
      } hint={t("Top 15 tables; darker bars are tables listed under “needs attention”. Click a bar for details.")}>
        <BarList data={series} max={metric === "fill" ? 100 : undefined} onPick={setPick} empty={t("No user tables.")} />
      </Section>
      <Section title={t("Index depth")} hint={t("Indices deeper than 3 levels cost extra page reads per lookup; a larger page size reduces depth.")}>
        <BarList data={indexSeries} max={Math.max(4, totals.maxDepth)} empty={t("No indices.")} />
      </Section>
      {selected && <TableDetail t={selected} onClose={() => setPick(null)} />}
      <Section title={t("All tables")}>
        <div className="grid-wrap" style={{ maxHeight: 520 }}><table className="grid">
          <thead><tr><th>{t("Table")}</th><th>{t("Records")}</th><th>{t("Versions")}</th><th>{t("Max ver.")}</th><th>{t("Fragments")}</th><th>{t("Data pages")}</th><th>{t("Avg fill")}</th><th>{t("Fill distribution")}</th><th>{t("Size")}</th><th>{t("Indices")}</th></tr></thead>
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
        <div className="hint" style={{ marginTop: 6 }}>* {t("estimated from data and pointer pages (Firebird 3/4 do not report the table size).")}</div>
      </Section>
      <Section title={t("Raw output")}><details><summary>{t("Show gstat output")}</summary><pre className="log" style={{ maxHeight: 400 }}>{res.text}</pre></details></Section>
    </>
  );
}

function TableDetail(props: { t: GstatTable; onClose: () => void }) {
  const tb = props.t;
  return (
    <Section title={t("Table {name}", { name: tb.name })} right={<button className="btn small" onClick={props.onClose}>{t("Close")}</button>}>
      <div className="mt-row" style={{ alignItems: "flex-start", gap: 24 }}>
        <table className="kv" style={{ width: 360 }}><tbody>
          {[[t("Records"), tb.total_records], [t("Back versions"), tb.total_versions], [t("Max versions"), tb.max_versions],
            [t("Avg record length"), tb.average_record_length], [t("Avg version length"), tb.average_version_length],
            [t("Fragments"), tb.total_fragments], [t("Compression ratio"), tb.compression_ratio], [t("Data pages"), tb.data_pages],
            [t("Primary / secondary pages"), `${tb.primary_pages ?? "—"} / ${tb.secondary_pages ?? "—"}`], [t("Empty / full pages"), `${tb.empty_pages ?? "—"} / ${tb.full_pages ?? "—"}`],
            [t("Blobs"), tb.blobs ?? 0], [t("Formats"), t("{used} used of {total}", { used: tb.used_formats ?? "—", total: tb.total_formats ?? "—" })]].map(([k, v]) => (
            <tr key={String(k)}><td>{k}</td><td>{typeof v === "number" ? v.toLocaleString() : String(v ?? "—")}</td></tr>
          ))}
        </tbody></table>
        <div>
          <div className="muted" style={{ marginBottom: 4 }}>{t("Data page fill distribution")}</div>
          <FillHistogram counts={tb.fill_distribution} width={240} height={70} />
        </div>
      </div>
      {tb.indices.length > 0 && <>
        <h3>{t("Indices")}</h3>
        <div className="grid-wrap"><table className="grid">
          <thead><tr><th>{t("Index")}</th><th>{t("Depth")}</th><th>{t("Leaf buckets")}</th><th>{t("Nodes")}</th><th>{t("Avg key")}</th><th>{t("Total dup")}</th><th>{t("Max dup")}</th><th>{t("Clustering")}</th><th>{t("Fill distribution")}</th></tr></thead>
          <tbody>{tb.indices.map((i) => (
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

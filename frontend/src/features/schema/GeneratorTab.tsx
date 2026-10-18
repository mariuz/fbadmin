import { useEffect, useState } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import type { WorkTab } from "../../context";
import { IconPlay, IconSpinner, IconTable } from "../../components/Icons";
import { schemaApi } from "./api";
import { JobStatus, useJob } from "./common";
import { N_, t } from "../../i18n";

interface ColInfo {
  name: string; data_type: string; domain: string | null; not_null: boolean; computed: string | null; identity: string | null;
  primary_key: boolean;
}
type Override = { mode: string; value?: string; values?: string; min?: string; max?: string };

const MODES: Array<[string, string]> = [
  ["auto", N_("Automatic")], ["null", "NULL"], ["value", N_("Fixed value")], ["list", N_("One of a list")], ["range", N_("Number range")],
  ["sequence", N_("Sequence 1, 2, 3…")], ["skip", N_("Do not set (default)")],
];

export function GeneratorTab({ tab }: { tab: WorkTab }) {
  const app = useApp();
  const sid = tab.serverId!, dbid = tab.dbId!;
  const [tables, setTables] = useState<string[]>([]);
  const [table, setTable] = useState<string>(tab.props?.table ?? "");
  const [cols, setCols] = useState<ColInfo[]>([]);
  const [fks, setFks] = useState<Record<string, string>>({});
  const [checks, setChecks] = useState<string[]>([]);
  const [rows, setRows] = useState(100);
  const [nullRatio, setNullRatio] = useState(10);
  const [seed, setSeed] = useState("");
  const [ov, setOv] = useState<Record<string, Override>>({});
  const [error, setError] = useState<string | null>(null);
  const job = useJob();

  useEffect(() => {
    api.objects(sid, dbid, "tables").then((t) => setTables(t.map((x) => x.name))).catch((e) => setError(errMsg(e)));
  }, []);
  useEffect(() => {
    if (!table) return;
    setOv({});
    api.object(sid, dbid, "tables", table).then((o) => {
      setCols((o.columns ?? []) as ColInfo[]);
      const f: Record<string, string> = {};
      for (const c of o.constraints ?? []) if (c.type === "FOREIGN KEY") for (const x of c.columns) f[x] = `→ ${c.ref_table}`;
      setFks(f);
      setChecks((o.constraints ?? []).filter((c) => c.type === "CHECK").map((c) => c.source));
    }).catch((e) => setError(errMsg(e)));
  }, [table]);

  const setO = (name: string, patch: Partial<Override>) => setOv({ ...ov, [name]: { ...(ov[name] ?? { mode: "auto" }), ...patch } });
  const start = async () => {
    setError(null);
    const overrides: Record<string, unknown> = {};
    for (const [k, o] of Object.entries(ov)) {
      if (o.mode === "auto") continue;
      overrides[k] = { mode: o.mode, value: o.value, values: o.values, min: o.min, max: o.max };
    }
    try {
      job.start(await schemaApi.generate(sid, dbid, {
        table, rows, overrides, null_ratio: nullRatio / 100, seed: seed ? Number(seed) : null,
      }));
    } catch (e) {
      setError(errMsg(e));
    }
  };

  return (
    <div className="sch-col sch-scroll">
      <div className="sch-steps">
        <section>
          <h3>{t("Test data generator")}</h3>
          <div className="sch-row">
            <label>{t("Table")} <select className="text" value={table} onChange={(e) => setTable(e.target.value)}>
              <option value="">{t("— choose —")}</option>{tables.map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
            <label>{t("Rows")} <input className="text" type="number" min={1} style={{ width: 100 }} value={rows} onChange={(e) => setRows(Number(e.target.value) || 1)} /></label>
            <label>{t("NULLs in nullable columns")} <input className="text" type="number" min={0} max={100} style={{ width: 60 }} value={nullRatio} onChange={(e) => setNullRatio(Number(e.target.value))} /> %</label>
            <label>{t("Seed")} <input className="text" style={{ width: 80 }} placeholder={t("random")} value={seed} onChange={(e) => setSeed(e.target.value.replace(/\D/g, ""))} /></label>
          </div>
          <p className="muted">{t("Values follow the column type and domain; NOT NULL is respected, simple CHECK constraints (comparisons, BETWEEN, IN, LIKE) are honoured, foreign keys take values sampled from the parent table and primary/unique keys stay unique. Rows that still violate a constraint are skipped and reported.")}</p>
        </section>
        {table && (
          <section>
            <h3>{t("Columns")}</h3>
            <table className="sch-map">
              <thead><tr><th>{t("Column")}</th><th>{t("Type")}</th><th>{t("Rules")}</th><th>{t("Generator")}</th><th /></tr></thead>
              <tbody>
                {cols.map((c) => {
                  const o = ov[c.name] ?? { mode: "auto" };
                  const fixed = c.computed ? t("computed") : c.identity && o.mode === "auto" ? t("identity ({kind})", { kind: c.identity }) : null;
                  return (
                    <tr key={c.name} className={fixed ? "off" : ""}>
                      <td><b>{c.name}</b></td>
                      <td className="muted">{c.domain ? `${c.domain} (${c.data_type})` : c.data_type}</td>
                      <td className="muted">{[c.primary_key && "PK", c.not_null && "NOT NULL", fks[c.name]].filter(Boolean).join(" · ")}</td>
                      <td>{c.computed ? <span className="muted">{t("computed")}</span> : (
                        <select className="text" value={o.mode} onChange={(e) => setO(c.name, { mode: e.target.value })}>
                          {MODES.map(([k, l]) => <option key={k} value={k}>{k === "auto" && fixed ? t("Automatic: {value}", { value: fixed }) : t(l)}</option>)}
                        </select>
                      )}</td>
                      <td>
                        {o.mode === "value" && <input className="text" value={o.value ?? ""} onChange={(e) => setO(c.name, { value: e.target.value })} />}
                        {o.mode === "list" && <input className="text" placeholder="a, b, c" value={o.values ?? ""} onChange={(e) => setO(c.name, { values: e.target.value })} />}
                        {o.mode === "range" && <span className="sch-row" style={{ margin: 0 }}>
                          <input className="text" style={{ width: 80 }} placeholder={t("min")} value={o.min ?? ""} onChange={(e) => setO(c.name, { min: e.target.value })} />
                          <input className="text" style={{ width: 80 }} placeholder={t("max")} value={o.max ?? ""} onChange={(e) => setO(c.name, { max: e.target.value })} />
                        </span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {checks.length > 0 && <p className="muted">{t("Checks: {list}", { list: checks.join(" · ") })}</p>}
            <div className="sch-row">
              <button className="btn primary" disabled={!table || job.running} onClick={start}>{job.running ? <IconSpinner /> : <IconPlay />} {t("Generate {n} row(s)", { n: rows.toLocaleString() })}</button>
              {job.result?.status === "finished" && (
                <button className="btn" onClick={() => app.openTab({ kind: "data", title: table, serverId: sid, dbId: dbid, otype: "tables", name: table })}><IconTable /> {t("View data")}</button>
              )}
              {error && <span className="err">{error}</span>}
            </div>
            <JobStatus result={job.result} log={job.log} />
          </section>
        )}
      </div>
    </div>
  );
}

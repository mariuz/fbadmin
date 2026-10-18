import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../../api";
import { errMsg, useApp } from "../../context";
import type { Job } from "../../types";
import { schemaApi, type JobResult } from "./api";

export const quoteIdent = (n: string) => (/^[A-Z][A-Z0-9_$]*$/.test(n) ? n : `"${n.replace(/"/g, '""')}"`);

export function dbName(app: ReturnType<typeof useApp>, sid?: string, dbid?: string) {
  const s = app.servers.find((x) => x.id === sid);
  const d = s?.databases.find((x) => x.id === dbid);
  return d ? `${s!.name} / ${d.name}` : "?";
}

/** Server + database selects. */
export function DbPicker(props: {
  value: { server_id: string; db_id: string };
  onChange: (v: { server_id: string; db_id: string }) => void;
  label?: string;
}) {
  const app = useApp();
  const srv = app.servers.find((s) => s.id === props.value.server_id);
  return (
    <span className="sch-picker">
      {props.label && <b>{props.label}</b>}
      <select className="text" value={props.value.server_id}
        onChange={(e) => {
          const s = app.servers.find((x) => x.id === e.target.value);
          props.onChange({ server_id: e.target.value, db_id: s?.databases[0]?.id ?? "" });
        }}>
        {app.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <select className="text" value={props.value.db_id} onChange={(e) => props.onChange({ ...props.value, db_id: e.target.value })}>
        {(srv?.databases ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
    </span>
  );
}

/** Poll a schema job until it ends; returns the structured result and the log. */
export function useJob(onDone?: (r: JobResult) => void) {
  const app = useApp();
  const [job, setJob] = useState<Job | null>(null);
  const [result, setResult] = useState<JobResult | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const done = useRef(onDone);
  done.current = onDone;
  useEffect(() => {
    if (!job) return;
    let stop = false;
    const tick = async () => {
      try {
        const [r, j] = await Promise.all([schemaApi.jobResult(job.id), api.job(job.id)]);
        if (stop) return;
        setResult(r);
        setLog(j.log ?? []);
        if (r.status === "running") setTimeout(tick, 600);
        else done.current?.(r);
      } catch (e) {
        if (!stop) app.toast(errMsg(e), "error");
      }
    };
    tick();
    return () => { stop = true; };
  }, [job?.id]);
  const start = (j: Job) => {
    setResult({ status: "running", error: null });
    setLog([]);
    setJob(j);
    app.jobStarted();
  };
  return { job, result, log, start, running: result?.status === "running" };
}

export function JobStatus(props: { result: JobResult | null; log: string[]; children?: ReactNode }) {
  const r = props.result;
  if (!r) return null;
  return (
    <div className="sch-job">
      <div>
        <span className={"pill " + r.status}>{r.status}</span>{" "}
        {r.inserted !== undefined && <b>{r.inserted.toLocaleString()} row(s) inserted</b>}
        {!!r.failed && <span className="err"> · {r.failed} failed</span>}
        {r.rows !== undefined && <b>{r.rows.toLocaleString()} row(s) copied</b>}
        {props.children}
      </div>
      {r.error && <div className="err" style={{ whiteSpace: "pre-wrap" }}>{r.error}</div>}
      {!!r.errors?.length && (
        <div className="grid-wrap" style={{ maxHeight: 200 }}>
          <table className="grid"><thead><tr><th>Row</th><th>Error</th></tr></thead>
            <tbody>{r.errors.map((e, i) => <tr key={i}><td className="num">{e.row}</td><td title={e.message}>{e.message}</td></tr>)}</tbody>
          </table>
        </div>
      )}
      {props.log.length > 0 && <div className="log" style={{ maxHeight: 220 }}>{props.log.join("\n")}</div>}
    </div>
  );
}

// ---------------------------------------------------------- line diff
type Op = { kind: "same" | "add" | "del"; a?: string; b?: string };

/** LCS line diff of a (left) and b (right). */
export function diffLines(a: string[], b: string[]): Op[] {
  const n = a.length, m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: Op[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: "same", a: a[i], b: b[j] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ kind: "del", a: a[i++] });
    else out.push({ kind: "add", b: b[j++] });
  }
  while (i < n) out.push({ kind: "del", a: a[i++] });
  while (j < m) out.push({ kind: "add", b: b[j++] });
  return out;
}

/** Side-by-side DDL diff: left = source (desired), right = target (current). */
export function SideBySide(props: { left: string | null; right: string | null; leftTitle: string; rightTitle: string }) {
  const rows = useMemo(() => {
    const split = (s: string | null) => (s === null ? [] : s.replace(/\n$/, "").split("\n"));
    const ops = diffLines(split(props.left), split(props.right));
    // pair consecutive deletions and additions on the same row
    const out: Array<{ l?: string; r?: string; kind: string }> = [];
    for (let k = 0; k < ops.length;) {
      if (ops[k].kind === "same") { out.push({ l: ops[k].a, r: ops[k].b, kind: "same" }); k++; continue; }
      const dels: string[] = [], adds: string[] = [];
      while (k < ops.length && ops[k].kind !== "same") {
        if (ops[k].kind === "del") dels.push(ops[k].a!); else adds.push(ops[k].b!);
        k++;
      }
      for (let x = 0; x < Math.max(dels.length, adds.length); x++)
        out.push({ l: dels[x], r: adds[x], kind: "chg" });
    }
    return out;
  }, [props.left, props.right]);
  return (
    <div className="sch-sbs">
      <div className="sch-sbs-head"><div>{props.leftTitle}</div><div>{props.rightTitle}</div></div>
      <div className="sch-sbs-body">
        {rows.map((r, i) => (
          <div key={i} className={"sch-sbs-row " + r.kind}>
            <pre className={r.kind === "chg" && r.l !== undefined ? "l" : ""}>{r.l ?? (props.left === null ? "" : " ")}</pre>
            <pre className={r.kind === "chg" && r.r !== undefined ? "r" : ""}>{r.r ?? (props.right === null ? "" : " ")}</pre>
          </div>
        ))}
        {props.left === null && <div className="sch-sbs-note left">not in source</div>}
        {props.right === null && <div className="sch-sbs-note right">not in target</div>}
      </div>
    </div>
  );
}

export function Labeled(props: { label: string; children: ReactNode }) {
  return <label className="sch-labeled"><span>{props.label}</span>{props.children}</label>;
}

/** Graphical rendering of Firebird detailed ("explained") plans, and a line diff for comparing plans. */
import { useMemo } from "react";
import { t } from "../i18n";

export interface PlanNode {
  text: string;
  children: PlanNode[];
}

/** Parse the indented "-> ..." text of isc_info_sql_explain_plan into trees (one per Select Expression). */
export function parsePlan(text: string): PlanNode[] {
  const roots: PlanNode[] = [];
  const stack: Array<{ indent: number; node: PlanNode }> = [];
  for (const raw of text.split("\n")) {
    if (!raw.trim()) continue;
    const m = /^(\s*)(->\s*)?(.*)$/.exec(raw)!;
    const indent = m[1].length + (m[2] ? 0 : -1);
    const node: PlanNode = { text: m[3].trim(), children: [] };
    while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop();
    if (stack.length) stack[stack.length - 1].node.children.push(node);
    else roots.push(node);
    stack.push({ indent, node });
  }
  return roots;
}

function classify(text: string): { kind: string; title: string; detail: string } {
  // Firebird 6 qualifies the table with its schema: Table "PUBLIC"."T" as "A" Full Scan
  const tm = /^Table (?:"([^"]+)"\.)?"([^"]+)"(?: as "([^"]+)")?\s*(.*)$/.exec(text);
  if (tm) {
    const scan = tm[4] || "";
    const table = tm[1] && tm[1] !== "PUBLIC" ? `${tm[1]}.${tm[2]}` : tm[2];
    return { kind: /Full Scan/i.test(scan) ? "scan-full" : "table", title: tm[3] ? `${table} ${tm[3]}` : table, detail: scan };
  }
  const i = /^Index (?:"[^"]+"\.)?"([^"]+)"\s*(.*)$/.exec(text);
  if (i) return { kind: "index", title: i[1], detail: i[2] };
  const op = /^([A-Za-z][A-Za-z ]*?)(\s*\(.*\))?$/.exec(text);
  if (op) return { kind: /Sort|Aggregate|Hash/i.test(op[1]) ? "heavy" : "op", title: op[1], detail: (op[2] || "").trim() };
  return { kind: "op", title: text, detail: "" };
}

function Node(props: { node: PlanNode }) {
  const c = classify(props.node.text);
  return (
    <div className="pg-node">
      <div className={"pg-box pg-" + c.kind} title={props.node.text}>
        <div className="pg-title">{c.title}</div>
        {c.detail && <div className="pg-detail">{c.detail}</div>}
      </div>
      {props.node.children.length > 0 && (
        <div className="pg-children">
          {props.node.children.map((ch, i) => <Node key={i} node={ch} />)}
        </div>
      )}
    </div>
  );
}

export function PlanGraph(props: { text: string }) {
  const roots = useMemo(() => parsePlan(props.text), [props.text]);
  return (
    <div className="pg">
      {roots.map((r, i) => <Node key={i} node={r} />)}
      <div className="pg-legend">
        <span className="pg-box pg-scan-full">{t("full scan")}</span>
        <span className="pg-box pg-table">{t("table access")}</span>
        <span className="pg-box pg-index">{t("index")}</span>
        <span className="pg-box pg-heavy">{t("sort / aggregate / hash")}</span>
      </div>
    </div>
  );
}

/** Line diff (LCS). Returns rows of [left, right, kind]. */
export function diffLines(a: string, b: string): Array<[string | null, string | null, "same" | "del" | "add"]> {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) {
    dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  }
  const out: Array<[string | null, string | null, "same" | "del" | "add"]> = [];
  let i = 0, j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && x[i] === y[j]) { out.push([x[i], y[j], "same"]); i++; j++; }
    else if (j < m && (i >= n || dp[i][j + 1] >= dp[i + 1][j])) { out.push([null, y[j], "add"]); j++; }
    else { out.push([x[i], null, "del"]); i++; }
  }
  return out;
}

export function PlanDiff(props: { left: string; right: string; leftTitle: string; rightTitle: string }) {
  const rows = useMemo(() => diffLines(props.left, props.right), [props.left, props.right]);
  return (
    <table className="plan-diff">
      <thead><tr><th>{props.leftTitle}</th><th>{props.rightTitle}</th></tr></thead>
      <tbody>
        {rows.map(([l, r, k], i) => (
          <tr key={i} className={"pd-" + k}>
            <td>{l ?? ""}</td>
            <td>{r ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

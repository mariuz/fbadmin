/** Opt-in AI assistant pane for the Query Tool (enabled server-side with FBADMIN_AI_PROVIDER). */
import { useEffect, useState } from "react";
import { request } from "../api";
import { errMsg } from "../context";

export interface AiStatus { enabled: boolean; provider?: string; model?: string; reason?: string }
let statusCache: Promise<AiStatus> | null = null;
export function aiStatus(): Promise<AiStatus> {
  statusCache ??= request<AiStatus>("GET", "/api/ai/status").catch(() => ({ enabled: false }));
  return statusCache;
}

export function useAiStatus(): AiStatus | null {
  const [s, setS] = useState<AiStatus | null>(null);
  useEffect(() => { aiStatus().then(setS); }, []);
  return s;
}

export const AI_TASKS: Array<[string, string]> = [
  ["explain_plan", "Explain the plan"],
  ["suggest_indexes", "Suggest indices"],
  ["convert_sql", "Convert SQL to Firebird"],
  ["explain_error", "Explain the last error"],
  ["ask", "Ask a question…"],
];

export async function askAi(body: Record<string, unknown>): Promise<string> {
  const r = await request<{ answer: string }>("POST", "/api/ai/ask", body);
  return r.answer;
}

/** Render the answer: plain paragraphs plus ```sql blocks with an "Insert" button. */
export function AssistantAnswer(props: { text: string; onInsert: (sql: string) => void }) {
  const parts = props.text.split(/```(?:sql|SQL)?\n?([\s\S]*?)```/g);
  return (
    <div className="assistant">
      {parts.map((p, i) => i % 2 === 1 ? (
        <div key={i} className="ai-code">
          <pre className="mono">{p.trim()}</pre>
          <button className="btn small" onClick={() => props.onInsert(p.trim() + "\n")}>Insert into editor</button>
        </div>
      ) : p.trim() ? <div key={i} className="ai-text">{p.trim()}</div> : null)}
    </div>
  );
}

export function AssistantPane(props: {
  state: { loading: boolean; task?: string; answer?: string; error?: string } | null;
  status: AiStatus | null;
  onInsert: (sql: string) => void;
}) {
  const { state, status } = props;
  if (!status?.enabled) {
    return <div className="empty-state"><div className="big">AI assistant is off</div><div>{status?.reason ?? "Not configured."}</div>
      <div className="muted">Only SQL text, plans and table definitions are sent — never table data.</div></div>;
  }
  if (!state) return <div className="empty-state">Pick a task from the Assistant menu. Using {status.provider}{status.model ? ` · ${status.model}` : ""}.</div>;
  if (state.loading) return <div className="pad muted">Thinking about “{AI_TASKS.find(([k]) => k === state.task)?.[1]}”…</div>;
  if (state.error) return <div className="pad err">{state.error}</div>;
  return <AssistantAnswer text={state.answer ?? ""} onInsert={props.onInsert} />;
}

export { errMsg };

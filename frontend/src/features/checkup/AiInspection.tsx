/** AI inspection of a performance check-up: diagnosis and ranked recommendations from the assistant provider. */
import { useState } from "react";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { useAiStatus } from "../../components/Assistant";
import { Markdown } from "../../components/Markdown";
import { Section } from "../maintenance/common";
import { fmtTime, waitJob } from "../maintenance/api";
import { checkupApi, type AiItem, type CheckupReport } from "./api";

export function AiInspection(props: { serverId: string; dbId: string; report: CheckupReport; onDone: () => Promise<unknown> }) {
  const app = useApp();
  const status = useAiStatus();
  const { serverId: sid, dbId: dbid, report } = props;
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [sent, setSent] = useState<{ system: string; prompt: string } | null>(null);
  const items: AiItem[] = report.ai ?? [];

  const ask = async (q?: string) => {
    setError(null); setRunning(t("Starting…"));
    try {
      const job = await checkupApi.inspect(sid, dbid, q);
      const end = await waitJob(job.id, (j) => setRunning(j.log?.[j.log.length - 1] ?? t("Running…")));
      if (end.status === "failed") setError(end.error ?? t("The AI inspection failed"));
      else setQuestion("");
      await props.onDone();
    } catch (e) { setError(errMsg(e)); }
    setRunning(null);
  };

  const showSent = async () => {
    if (sent) { setSent(null); return; }
    try { setSent(await checkupApi.preview(sid, dbid)); } catch (e) { app.toast(errMsg(e), "error"); }
  };

  const using = status?.enabled ? `${status.provider ?? ""}${status.model ? ` · ${status.model}` : ""}` : "";
  return (
    <Section wide title={t("AI inspection")}
      hint={t("The AI assistant reads this report and explains why the database is slow: a diagnosis that connects the findings, recommendations ranked by impact with the SQL or settings to apply, and what to check next. Check its advice before applying it.")}
      right={status?.enabled ? <>
        <button className="btn small" aria-expanded={!!sent} onClick={showSent}>{t("What is sent…")}</button>
        <button className="btn primary small" disabled={!!running} onClick={() => ask()}>{items.length ? t("Inspect again") : t("Inspect with AI")}</button>
      </> : undefined}>
      {status && !status.enabled && <div className="muted">
        {t("The AI assistant is off.")} {status.reason ?? ""}
      </div>}
      {status?.enabled && <div className="muted cu-note">
        {t("Sends the findings, statistics, server settings, statement texts, plans and table definitions to {provider} — never table data; IP addresses are masked.", { provider: using })}
      </div>}
      {sent && <pre className="code-block cu-sent" tabIndex={0} aria-label={t("What is sent")}>{sent.system + "\n\n―――\n\n" + sent.prompt}</pre>}
      {running && <div className="muted" role="status">{t("Thinking…")} {running}</div>}
      {error && <div className="err">{error}</div>}
      {items.map((it, i) => (
        <div key={i} className="cu-ai">
          {it.question && <div className="cu-q"><strong>{t("Question")}:</strong> {it.question}</div>}
          <Markdown text={it.answer} onOpenSql={(sql) => app.openQueryTool(sid, dbid, sql)} />
          <div className="muted cu-meta">{[it.provider, it.model, fmtTime(it.at)].filter(Boolean).join(" · ")}</div>
        </div>
      ))}
      {status?.enabled && items.length > 0 && (
        <form className="mt-row cu-follow" onSubmit={(e) => { e.preventDefault(); if (question.trim()) ask(question); }}>
          <label className="sr-only" htmlFor="cu-question">{t("Ask a follow-up question")}</label>
          <input id="cu-question" className="text" placeholder={t("Ask a follow-up question, e.g. how big should the cache be?")}
            value={question} onChange={(e) => setQuestion(e.target.value)} disabled={!!running} />
          <button className="btn small" type="submit" disabled={!!running || !question.trim()}>{t("Ask")}</button>
        </form>
      )}
    </Section>
  );
}

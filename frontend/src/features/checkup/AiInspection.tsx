/** AI inspection of a performance check-up: diagnosis and ranked recommendations from the assistant provider. */
import { Fragment, useState, type ReactNode } from "react";
import { errMsg, useApp } from "../../context";
import { t } from "../../i18n";
import { useAiStatus } from "../../components/Assistant";
import { IconCopy, IconTerminal } from "../../components/Icons";
import { Section } from "../maintenance/common";
import { fmtTime, waitJob } from "../maintenance/api";
import { checkupApi, type AiItem, type CheckupReport } from "./api";

/** **bold** and `code` inside a line. */
function inline(text: string): ReactNode[] {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((p, i) =>
    p.startsWith("`") && p.endsWith("`") && p.length > 1 ? <code key={i}>{p.slice(1, -1)}</code>
      : p.startsWith("**") && p.endsWith("**") && p.length > 3 ? <strong key={i}>{p.slice(2, -2)}</strong>
        : <Fragment key={i}>{p}</Fragment>);
}

/** A small Markdown subset: headings, lists, paragraphs, code blocks. Built as React elements (no HTML). */
function Markdown(props: { text: string; onOpenSql: (sql: string) => void }) {
  const app = useApp();
  const parts = props.text.split(/```([a-zA-Z]*)\n?([\s\S]*?)```/g);
  const out: ReactNode[] = [];
  for (let i = 0; i < parts.length; i += 3) {
    const lines = parts[i].split("\n");
    let list: { ordered: boolean; items: string[] } | null = null;
    let para: string[] = [];
    const flush = () => {
      if (para.length) out.push(<p key={out.length}>{inline(para.join(" "))}</p>);
      para = [];
      if (list) {
        const items = list.items.map((x, k) => <li key={k}>{inline(x)}</li>);
        out.push(list.ordered ? <ol key={out.length}>{items}</ol> : <ul key={out.length}>{items}</ul>);
      }
      list = null;
    };
    for (const raw of lines) {
      const line = raw.trimEnd();
      const h = /^#{1,4}\s+(.*)$/.exec(line);
      const li = /^\s*(?:([-*])|(\d+)[.)])\s+(.*)$/.exec(line);
      if (!line.trim()) { flush(); continue; }
      if (h) { flush(); out.push(<h5 key={out.length}>{inline(h[1])}</h5>); continue; }
      if (li) {
        const ordered = !!li[2];
        if (para.length || (list && list.ordered !== ordered)) flush();
        list ??= { ordered, items: [] };
        list.items.push(li[3]);
        continue;
      }
      if (list && /^\s+\S/.test(raw)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
      if (list) flush();
      para.push(line.trim());
    }
    flush();
    if (i + 2 < parts.length) {
      const lang = parts[i + 1].toLowerCase(), code = parts[i + 2].trim();
      out.push(
        <div key={out.length} className="ai-code">
          <pre className="mono" tabIndex={0} aria-label={lang === "sql" ? t("SQL") : t("Code")}>{code}</pre>
          <div className="mt-row">
            {(lang === "sql" || lang === "") && <button className="btn small" onClick={() => props.onOpenSql(code + "\n")}><IconTerminal size={12} /> {t("Open in Query Tool")}</button>}
            <button className="btn small" onClick={() => { navigator.clipboard?.writeText(code); app.toast(t("Copied"), "info"); }}><IconCopy size={12} /> {t("Copy")}</button>
          </div>
        </div>,
      );
    }
  }
  return <div className="cu-md">{out}</div>;
}

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

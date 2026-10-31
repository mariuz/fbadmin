/** A small Markdown subset (headings, lists, paragraphs, **bold**, `code`, fenced code blocks), built as React
 *  elements, never as HTML. Used for AI answers and notebook notes. */
import { Fragment, type ReactNode } from "react";
import { useApp } from "../context";
import { t } from "../i18n";
import { IconCopy, IconTerminal } from "./Icons";
import "./markdown.css";

/** **bold** and `code` inside a line. */
function inline(text: string): ReactNode[] {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((p, i) =>
    p.startsWith("`") && p.endsWith("`") && p.length > 1 ? <code key={i}>{p.slice(1, -1)}</code>
      : p.startsWith("**") && p.endsWith("**") && p.length > 3 ? <strong key={i}>{p.slice(2, -2)}</strong>
        : <Fragment key={i}>{p}</Fragment>);
}

/** A small Markdown subset: headings, lists, paragraphs, code blocks. Built as React elements (no HTML). */
export function Markdown(props: { text: string; onOpenSql?: (sql: string) => void }) {
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
            {props.onOpenSql && (lang === "sql" || lang === "") && <button className="btn small" onClick={() => props.onOpenSql?.(code + "\n")}><IconTerminal size={12} /> {t("Open in Query Tool")}</button>}
            <button className="btn small" onClick={() => { navigator.clipboard?.writeText(code); app.toast(t("Copied"), "info"); }}><IconCopy size={12} /> {t("Copy")}</button>
          </div>
        </div>,
      );
    }
  }
  return <div className="md">{out}</div>;
}


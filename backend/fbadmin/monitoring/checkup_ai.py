"""AI inspection of a performance check-up (opt-in, uses the assistant provider of :mod:`fbadmin.ai`).

The model gets the last check-up report of a database and what explains it: the ranked findings with
their evidence, the key numbers, the server settings that matter for performance, the largest tables
from gstat, the slowest statements with their plans (history store), and the definitions of the tables
the findings and statements touch. It answers with a diagnosis that connects the findings, a ranked
plan of concrete changes (SQL and settings) and what to check next. Follow-up questions see the
earlier answers.

Like the rest of the assistant: SQL text, plans, statistics and table definitions are sent, never
table data. IP addresses are masked. ``preview`` returns exactly what would be sent.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..metadata import Catalog
from . import checkup

MAX_DDL_TABLES = 8
MAX_DDL_CHARS = 4000
MAX_STATEMENTS = 10
MAX_SQL_CHARS = 1500
MAX_FOLLOWUPS = 5

SYSTEM = (
    "You are a senior Firebird performance engineer embedded in fbadmin, a database administration tool. "
    "You inspect the result of fbadmin's performance check-up of one Firebird {version} database and tell "
    "the administrator why it is slow and what to do, most effective first.\n"
    "Rules:\n"
    "- Work from the data given. Connect the findings (for example a stuck oldest active transaction "
    "explains record versions, and outdated index selectivity explains a regressed statement). Do not "
    "repeat each finding; explain what matters and why.\n"
    "- Be specific to Firebird {version}: its optimizer, garbage collection and sweep, page cache, lock "
    "manager, firebird.conf / databases.conf settings and SQL dialect.\n"
    "- Refer to findings by their number, e.g. (finding 2).\n"
    "- Put every statement the administrator can run in a ```sql block, one purpose per block. Put "
    "configuration changes in a ```conf block with the file name in a comment.\n"
    "- Never invent tables, columns, indices or settings that are not in the data; say what is missing "
    "instead. Say how sure you are when the data is thin.\n"
    "- Answer in Markdown with exactly these sections:\n"
    "## Diagnosis\n(2-6 sentences: the most likely reasons it is slow)\n"
    "## Recommendations\n(a numbered list, most impact first; each item: what to do, why (which "
    "findings), how (SQL or setting), and the risk or cost, e.g. needs exclusive access, locks a table, "
    "takes long on large tables)\n"
    "## What to check next\n(short list of measurements that would confirm or rule out the diagnosis)\n"
    "For a follow-up question, answer only the question, in the same style."
)

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d+)?\b")
# needs "::" or a hex letter, so times such as 10:30:00 are left alone
_IPV6 = re.compile(r"(?<![\w:])(?=[0-9A-Fa-f:]*(?:::|[A-Fa-f]))[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")


def redact(text: str) -> str:
    """Mask IP addresses (they say nothing about performance)."""
    return _IPV6.sub("<ip>", _IPV4.sub("<ip>", text or ""))


def _clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …(truncated)"


# ===================================================================== context
def _findings_text(report: dict) -> str:
    lines = []
    for f in report["findings"]:
        t = f.get("text") or {}
        lines.append(f"{f['rank']}. [{f['severity']}] {t.get('title', f['title'])}")
        lines.append(f"   fbadmin's advice: {t.get('advice', f['advice'])}")
        for e in f.get("evidence") or []:
            row = f"   - {e.get('label')}: {e.get('value') or ''}".rstrip(": ")
            lines.append(row)
            if e.get("sql"):
                lines.append("     SQL: " + _clip(e["sql"], MAX_SQL_CHARS).replace("\n", "\n          "))
        acts = [a["label"].format(**{k: v for k, v in a["params"].items() if isinstance(v, (str, int, float))})
                for a in f.get("actions") or [] if a["kind"] != "open"]
        if acts:
            lines.append("   fix buttons offered: " + "; ".join(acts))
    return "\n".join(lines) or "(no findings: fbadmin found nothing that slows this database down)"


def _settings(conn, cat: Catalog) -> dict[str, Any]:
    """Performance-related server settings (RDB$CONFIG, Firebird 4+)."""
    try:
        if not cat.one("SELECT COUNT(*) AS n FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = 'RDB$CONFIG'",
                       scoped=False)["n"]:
            return {}
        names = ("ServerMode", "DefaultDbCachePages", "TempCacheLimit", "LockMemSize", "LockHashSlots",
                 "DeadlockTimeout", "GCPolicy", "MaxParallelWorkers", "ParallelWorkers", "UseFileSystemCache",
                 "FileSystemCacheThreshold", "InlineSortThreshold", "TempBlockSize", "MaxUnflushedWrites",
                 "MaxUnflushedWriteTime", "StatementTimeout", "ConnectionIdleTimeout", "OptimizeForFirstRows",
                 "MaxStatementCacheSize", "CpuAffinityMask")
        rows = cat.rows("SELECT TRIM(RDB$CONFIG_NAME) AS name, RDB$CONFIG_VALUE AS config_value, "
                        "RDB$CONFIG_IS_SET AS is_set FROM RDB$CONFIG", scoped=False)
        return {r["name"]: (r["config_value"] or "").strip() + ("" if r["is_set"] else " (default)")
                for r in rows if r["name"] in names}
    except Exception:  # noqa: BLE001 - no privilege on RDB$CONFIG
        return {}


def _database_props(cat: Catalog) -> dict[str, Any]:
    try:
        d = cat.one("SELECT MON$FORCED_WRITES AS forced_writes, MON$RESERVE_SPACE AS reserve_space, "
                    "MON$SQL_DIALECT AS dialect, MON$ODS_MAJOR AS ods_major, MON$ODS_MINOR AS ods_minor, "
                    "MON$READ_ONLY AS db_read_only FROM MON$DATABASE", scoped=False) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {"forced_writes": bool(d.get("forced_writes")), "reserve_space": bool(d.get("reserve_space")),
            "sql_dialect": d.get("dialect"), "ods": f"{d.get('ods_major')}.{d.get('ods_minor')}",
            "read_only": bool(d.get("db_read_only"))}


def _slow_statements(server_id: str, db_id: str) -> list[dict[str, Any]]:
    from . import history

    out = []
    for s in history.get_store().slow_statements(server_id, db_id, MAX_STATEMENTS):
        out.append({"seconds": round(s["max_seconds"] or 0, 2), "seen": s["seen"],
                    "sql": _clip(s["sql_text"], MAX_SQL_CHARS), "plan": _clip(s["plan"], 1500)})
    return out


def _tables_named(cat: Catalog, texts: list[str]) -> list[str]:
    try:
        tables = {r["name"] for r in cat.list_objects("tables")}
    except Exception:  # noqa: BLE001
        return []
    found: list[str] = []
    for text in texts:
        for w in re.findall(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*', text or ""):
            n = w.strip('"') if w.startswith('"') else w.upper()
            if n in tables and n not in found:
                found.append(n)
    return found


def build(server_id: str, db_id: str, report: dict, question: str | None = None,
          previous: list[dict] | None = None) -> tuple[str, str]:
    """(system prompt, user prompt) for the report of this database."""
    from ..api.deps import get_state

    st = get_state()
    parts: list[str] = []
    with st.manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        version = cat.engine_version or "3.0+"
        settings = _settings(conn, cat)
        props = _database_props(cat)
        statements = _slow_statements(server_id, db_id)
        texts = [json.dumps(report["findings"])] + [s["sql"] for s in statements] + \
                [t["name"] for t in report.get("gstat_tables") or []]
        ddl = []
        for name in _tables_named(cat, texts)[:MAX_DDL_TABLES]:
            try:
                ddl.append(_clip(cat.get_object("tables", name)["ddl"], MAX_DDL_CHARS))
            except Exception:  # noqa: BLE001
                pass
        try:
            from ..query import end_transaction

            end_transaction(conn, commit=False)
        except Exception:  # noqa: BLE001
            pass

    facts = {k: v for k, v in (report.get("facts") or {}).items() if v is not None}
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.get("generated_at") or time.time()))
    parts.append(f"Check-up of database \"{report.get('database')}\" on Firebird {version}, run {when} "
                 f"({'with' if report.get('gstat') else 'without'} gstat).")
    parts.append("Key numbers:\n" + json.dumps(facts | props, indent=1, default=str))
    if settings:
        parts.append("Server settings (RDB$CONFIG):\n" + json.dumps(settings, indent=1))
    errors = [c for c in report.get("checks") or [] if c.get("status") == "error"]
    if errors:
        parts.append("Checks that failed: " + "; ".join(f"{c['name']}: {c.get('error')}" for c in errors))
    parts.append("Findings, most important first:\n" + _findings_text(report))
    if report.get("gstat_tables"):
        parts.append("Largest tables (gstat):\n" + json.dumps(report["gstat_tables"], indent=None, default=str))
    if statements:
        parts.append("Slowest statements seen by the history sampler (longest run in seconds, times seen, plan):\n"
                     + "\n\n".join(f"- {s['seconds']} s, seen {s['seen']}×\n  SQL: {s['sql']}\n  Plan: {s['plan'] or '?'}"
                                   for s in statements))
    elif not facts.get("tracked"):
        parts.append("History is not recorded for this database, so there are no slow statements or "
                     "query regressions to look at.")
    if ddl:
        parts.append("Definitions of the tables involved:\n" + "\n\n".join(ddl))
    for p in (previous or [])[-MAX_FOLLOWUPS:]:
        parts.append(("Earlier question: " + p["question"] + "\n" if p.get("question") else
                      "Your earlier inspection:\n") + p["answer"])
    if question and question.strip():
        parts.append("Follow-up question from the administrator: " + question.strip())
    else:
        parts.append("Inspect this check-up and give your diagnosis and recommendations.")
    return SYSTEM.format(version=version), redact("\n\n".join(parts))


# ===================================================================== running
def _report(server_id: str, db_id: str) -> dict:
    report = checkup.last(server_id, db_id)["report"]
    if not report:
        raise ValueError("Run a performance check-up of this database first")
    return report


def preview(server_id: str, db_id: str, question: str | None = None) -> dict[str, str]:
    report = _report(server_id, db_id)
    follow = bool(question and question.strip())
    system, prompt = build(server_id, db_id, report, question, report.get("ai") if follow else None)
    return {"system": system, "prompt": prompt}


def start(server_id: str, db_id: str, question: str | None = None) -> dict[str, Any]:
    """Ask the provider about the last report, as a job; the answer is added to the report's ``ai`` list."""
    from ..ai import AIUnavailable, get_provider
    from ..api.deps import get_state

    provider = get_provider()  # raises AIUnavailable for a broken configuration
    if provider is None:
        raise AIUnavailable("The AI assistant is off: set FBADMIN_AI_PROVIDER (for example anthropic)")
    report = _report(server_id, db_id)
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    generated = report["generated_at"]

    def run(log) -> None:
        log("Collecting the context…")
        follow = bool(question and question.strip())
        system, prompt = build(server_id, db_id, report, question, report.get("ai") if follow else None)
        meta = provider.describe()
        log(f"Asking {meta.get('provider')}" + (f" · {meta['model']}" if meta.get("model") else "") + "…")
        answer = provider.complete(system, prompt)
        item = {"question": question.strip() if follow else None, "answer": answer, "at": time.time(),
                "provider": meta.get("provider"), "model": meta.get("model")}
        with checkup._lock:
            cur = checkup._reports.get((server_id, db_id))
            if cur is None or cur["generated_at"] != generated:
                raise RuntimeError("A newer check-up replaced the report; ask again")
            cur["ai"] = ((cur.get("ai") or []) if follow else []) + [item]
        log("Done")

    return st.jobs.start("checkup_ai", f"AI inspection {db.name}", server_id, run).summary()

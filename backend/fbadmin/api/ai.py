"""AI assistant endpoints (opt-in, see fbadmin/ai)."""

from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..ai import AIUnavailable, get_provider
from ..metadata import Catalog
from ..query import explain
from ..sqlsplit import bind_params
from .deps import get_state

router = APIRouter(prefix="/api/ai", tags=["ai"])

SYSTEM = (
    "You are a Firebird SQL expert embedded in fbadmin, a database administration tool. "
    "Answer for Firebird {version} specifically (its SQL dialect, PSQL, plans and index behaviour). "
    "Be concise and practical: short explanation, then concrete SQL in ```sql blocks when relevant. "
    "Never invent tables or columns that are not in the provided DDL; say what is missing instead."
)

TASKS = {
    "explain_plan": "Explain this query's execution plan in plain language. Point out full table scans "
                    "(NATURAL / Full Scan), sorts and joins that could be slow, and why the optimizer chose them.",
    "suggest_indexes": "Suggest indices (CREATE INDEX statements) that would make this query faster, given the plan "
                       "and the table definitions. Mention selectivity concerns and existing indices that already cover it. "
                       "If no index would help, say so.",
    "convert_sql": "Convert the following SQL from {dialect} to Firebird SQL. Output the converted SQL in one ```sql block, "
                   "then list the changes you made (e.g. LIMIT -> FETCH FIRST/ROWS, AUTO_INCREMENT -> IDENTITY, "
                   "string concatenation, date functions, data types).",
    "explain_error": "Explain this Firebird error for the given SQL and show the corrected SQL.",
    "ask": "Answer the user's question about this SQL / database.",
}


class AskIn(BaseModel):
    task: str
    sql: str = ""
    question: str = ""
    error: str = ""
    dialect: str = "MySQL"
    server_id: str | None = None
    database_id: str | None = None


@router.get("/status")
def status():
    try:
        p = get_provider()
    except AIUnavailable as exc:
        return {"enabled": False, "reason": str(exc)}
    if p is None:
        return {"enabled": False, "reason": "Set FBADMIN_AI_PROVIDER=anthropic to enable the assistant"}
    return {"enabled": True, **p.describe()}


def _context(server_id: str, db_id: str, sql: str, with_plan: bool) -> tuple[str, str]:
    """Plan + DDL of the tables the statement touches; and the engine version."""
    parts: list[str] = []
    with get_state().manager.meta(server_id, db_id) as conn:
        cat = Catalog(conn)
        version = cat.engine_version
        names: list[str] = []
        if with_plan and sql.strip():
            bound, _ = bind_params(sql.strip().rstrip(";"), [0])
            try:
                plan = explain(conn, bound)
                detailed = explain(conn, bound, detailed=True)
                parts.append(f"Plan:\n{plan}\n\nDetailed plan:\n{detailed}")
                names += re.findall(r'Table "([^"]+)"', detailed or "")
            except Exception as exc:  # noqa: BLE001
                parts.append(f"(could not prepare the statement: {exc})")
        tables = {r["name"] for r in cat.list_objects("tables")} | {r["name"] for r in cat.list_objects("views")}
        names += [w for w in re.findall(r'[A-Za-z_][A-Za-z0-9_$]*|"[^"]+"', sql) if w.strip('"').upper() in tables or w.strip('"') in tables]
        seen = []
        for n in names:
            n = n.strip('"')
            n = n if n in tables else n.upper()
            if n in tables and n not in seen:
                seen.append(n)
        for n in seen[:12]:
            otype = "tables" if n in {r["name"] for r in cat.list_objects("tables")} else "views"
            try:
                parts.append(cat.get_object(otype, n)["ddl"])
            except Exception:  # noqa: BLE001
                pass
    return "\n\n".join(parts), version


@router.post("/ask")
def ask(body: AskIn):
    try:
        provider = get_provider()
        if provider is None:
            return JSONResponse(status_code=400, content={"detail": "AI assistant is disabled (FBADMIN_AI_PROVIDER not set)"})
        if body.task not in TASKS:
            return JSONResponse(status_code=400, content={"detail": f"Unknown task {body.task!r}"})
        ctx, version = "", "3.0+"
        if body.server_id and body.database_id and body.task != "convert_sql":
            ctx, version = _context(body.server_id, body.database_id, body.sql,
                                    with_plan=body.task in ("explain_plan", "suggest_indexes"))
        prompt = TASKS[body.task].format(dialect=body.dialect)
        if body.question:
            prompt += f"\n\nQuestion: {body.question}"
        if body.error:
            prompt += f"\n\nError:\n{body.error}"
        prompt += f"\n\nSQL:\n```sql\n{body.sql}\n```"
        if ctx:
            prompt += f"\n\nContext from the database:\n{ctx}"
        answer = provider.complete(SYSTEM.format(version=version or "3.0+"), prompt)
        return {"answer": answer, **provider.describe()}
    except AIUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

"""Database side of the PSQL debugging help: routine metadata, trace runs, profiled runs and tests.

Every run uses its own attachment and ONE transaction that is rolled back at the end, so writes done
by the routine are undone. What a rollback cannot undo (``IN AUTONOMOUS TRANSACTION``, ``EXECUTE
STATEMENT ... ON EXTERNAL``) is refused in the traced routine and reported as a warning when found
in a routine or trigger it depends on; sequence increments are reported as a warning.
"""

from __future__ import annotations

import decimal
import re
import time
from collections import deque
from typing import Any

import firebirdsql.fbcore as fbcore

from ..metadata import Catalog
from ..metadata.types import quote_ident
from ..query import end_transaction, error_message
from ..serialize import to_json
from . import psql
from .psql import PsqlError, TraceVar, Var

MAX_STEPS = 10000
MAX_STEPS_LIMIT = 100000
MAX_ROWS = 1000
VALUE_CHARS = 300  # longer values are cut in the step list
TEXT_CHARS = 1000  # long strings are traced as their first N characters
LONG_STRING_BYTES = 4000
MESSAGE_BUDGET = 48000  # bytes of trace columns (Firebird messages are limited to 64 KB)
FB3_SQL_LIMIT = 65535  # Firebird 3 limits statements to 64 KB

_SQL_TYPE_TEXT, _SQL_TYPE_VARYING, _SQL_TYPE_BLOB = 452, 448, 520
_BLOCK_LINE = re.compile(r"At block line: (\d+), col: (\d+)")


class RunError(ValueError):
    """A run could not be started (bad input, routine not traceable)."""


# ------------------------------------------------------------------ metadata
def _params_proc(cat: Catalog, name: str) -> tuple[list[Var], list[Var]]:
    ins, outs = [], []
    for p in cat.procedure_params(name):
        v = psql.var_from_decl(p["name"], p["data_type"], "input" if p["direction"] == "IN" else "output")
        (ins if p["direction"] == "IN" else outs).append(v)
    return ins, outs


def routine(cat: Catalog, otype: str, name: str) -> dict[str, Any]:
    """Everything the debugger needs about a stored procedure or function."""
    if otype == "procedures":
        r = cat.one(
            "SELECT RDB$PROCEDURE_SOURCE AS source, RDB$PROCEDURE_TYPE AS ptype, TRIM(RDB$ENGINE_NAME) AS engine, "
            "RDB$VALID_BLR AS valid" + cat.sql_security_col("RDB$PROCEDURES") +
            " FROM RDB$PROCEDURES WHERE RDB$PROCEDURE_NAME = ? AND RDB$PACKAGE_NAME IS NULL", (name,))
        if not r:
            raise LookupError(f"Procedure {name!r} not found")
        ins, outs = _params_proc(cat, name)
        source = r["source"] or ""
        kind = "selectable" if r["ptype"] == 1 else "executable"
        if r["ptype"] not in (1, 2) and not r["engine"]:
            try:
                kind = "selectable" if psql.parse_source(source).has_suspend() else "executable"
            except PsqlError:
                pass
        returns = None
    elif otype == "functions":
        r = cat.one(
            "SELECT RDB$FUNCTION_SOURCE AS source, TRIM(RDB$ENGINE_NAME) AS engine, RDB$LEGACY_FLAG AS legacy, "
            "RDB$RETURN_ARGUMENT AS ret, RDB$VALID_BLR AS valid" + cat.sql_security_col("RDB$FUNCTIONS") +
            " FROM RDB$FUNCTIONS WHERE RDB$FUNCTION_NAME = ? AND RDB$PACKAGE_NAME IS NULL", (name,))
        if not r:
            raise LookupError(f"Function {name!r} not found")
        if r["legacy"]:
            raise RunError("Legacy UDFs (DECLARE EXTERNAL FUNCTION) cannot be debugged or tested")
        args = cat.function_args(name)
        ret = r["ret"] or 0
        ins = [psql.var_from_decl(a["name"], a["data_type"], "input") for a in args if a["position"] != ret]
        returns = next((psql.bare_type(a["data_type"]) for a in args if a["position"] == ret), None)
        outs = []
        source = r["source"] or ""
        kind = "function"
    else:
        raise RunError("Only procedures and functions can be debugged")
    info: dict[str, Any] = {
        "otype": otype, "name": name, "kind": kind,
        "inputs": [_var_json(v) for v in ins], "outputs": [_var_json(v) for v in outs], "returns": returns,
        "source": None if r["engine"] else source, "external": bool(r["engine"]), "engine": r["engine"],
        "valid": r.get("valid"), "sql_security": (r.get("sql_security") or None),
        "traceable": False, "reason": None, "locals": [], "warnings": [],
    }
    if r["engine"]:
        info["reason"] = (f"{name} is an external routine ({r['engine']}): there is no PSQL to trace, "
                          "but it can be run and tested.")
    else:
        try:
            parsed = psql.parse_source(source)
            info["locals"] = [_var_json(v) for v in parsed.locals]
            info["warnings"] = psql.check_side_effects(parsed.toks)
            info["traceable"] = True
        except PsqlError as exc:
            info["reason"] = str(exc)
            info["error_line"] = exc.line
    if info["sql_security"] == "DEFINER":
        info["warnings"].append("SQL SECURITY DEFINER: the trace runs with YOUR privileges, not the owner's "
                                "(tests and profiled runs call the routine itself and are not affected).")
    return info


def _var_json(v: Var) -> dict[str, Any]:
    return {"name": v.name, "type": v.decl, "kind": v.kind}


def _vars(info: dict[str, Any], key: str) -> list[Var]:
    return [psql.var_from_decl(p["name"], p["type"], p["kind"]) for p in info[key]]


def block_info(sql: str) -> dict[str, Any]:
    """Debugger info for an anonymous ``EXECUTE BLOCK`` (inputs are its ``= ?`` parameters)."""
    parsed = psql.parse_execute_block(sql)
    return {
        "otype": "block", "name": None, "kind": "selectable" if parsed.has_suspend() else "executable",
        "inputs": [_var_json(v) for v in parsed.inputs], "outputs": [_var_json(v) for v in parsed.outputs],
        "returns": None, "source": sql, "external": False, "traceable": True, "reason": None,
        "locals": [_var_json(v) for v in parsed.locals], "warnings": psql.check_side_effects(parsed.toks),
        "sql_security": None,
    }


def dependency_warnings(cat: Catalog, otype: str, name: str, limit: int = 150) -> list[str]:
    """Look through called routines and fired triggers for effects a rollback does not undo."""
    dep_type = {"procedures": 5, "functions": 15}.get(otype)
    if dep_type is None:
        return []
    out: list[str] = []
    seen: set[tuple[str, int]] = {(name, dep_type)}
    todo = deque([(name, dep_type)])
    while todo and len(seen) < limit:
        nm, tp = todo.popleft()
        rows = cat.rows(
            "SELECT DISTINCT TRIM(RDB$DEPENDED_ON_NAME) AS name, RDB$DEPENDED_ON_TYPE AS dtype FROM RDB$DEPENDENCIES "
            "WHERE RDB$DEPENDENT_NAME = ? AND RDB$DEPENDENT_TYPE = ? AND RDB$DEPENDED_ON_TYPE IN (0, 5, 15)",
            (nm, tp))
        for d in rows:
            key = (d["name"], d["dtype"])
            if key in seen or d["name"].startswith("RDB$"):
                continue
            seen.add(key)
            if d["dtype"] == 0:
                rel = cat.one("SELECT RDB$EXTERNAL_FILE AS ext FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?",
                              (d["name"],))
                if rel and rel["ext"]:
                    out.append(f"{d['name']} is an external table: writes to it are NOT rolled back.")
                for trg in cat.rows("SELECT TRIM(RDB$TRIGGER_NAME) AS name, RDB$TRIGGER_SOURCE AS src FROM "
                                    "RDB$TRIGGERS WHERE RDB$RELATION_NAME = ? AND RDB$TRIGGER_INACTIVE = 0",
                                    (d["name"],)):
                    out += _source_warnings(f"trigger {trg['name']} (on {d['name']})", trg["src"])
                    if (trg["name"], 2) not in seen:
                        seen.add((trg["name"], 2))
                        todo.append((trg["name"], 2))
                continue
            table, col, src = (("RDB$PROCEDURES", "RDB$PROCEDURE", "RDB$PROCEDURE_SOURCE") if d["dtype"] == 5
                               else ("RDB$FUNCTIONS", "RDB$FUNCTION", "RDB$FUNCTION_SOURCE"))
            r = cat.one(f"SELECT {src} AS src, TRIM(RDB$ENGINE_NAME) AS engine FROM {table} WHERE {col}_NAME = ? "
                        "AND RDB$PACKAGE_NAME IS NULL", (d["name"],))
            if not r:
                continue
            what = ("procedure " if d["dtype"] == 5 else "function ") + d["name"]
            if r["engine"]:
                out.append(f"{what} is an external routine ({r['engine']}): effects outside the database are "
                           "not undone.")
            else:
                out += _source_warnings(what, r["src"])
            todo.append(key)
    return list(dict.fromkeys(out))


def _source_warnings(what: str, src: str | None) -> list[str]:
    if not src:
        return []
    try:
        psql.check_side_effects(psql.tokenize(src))
    except PsqlError as exc:
        if exc.code == "autonomous":
            return [f"{what} uses IN AUTONOMOUS TRANSACTION: its changes are committed and NOT rolled back."]
        if exc.code == "external":
            return [f"{what} uses EXECUTE STATEMENT ... ON EXTERNAL: changes in the other database are NOT "
                    "rolled back."]
    return []


# ------------------------------------------------------------------ helpers
def _schema(conn) -> None:
    """Firebird 6: resolve unqualified names in the active schema (as the metadata connections do)."""
    from ..schema_ctx import current_schema

    schema = current_schema.get()
    if schema and Catalog(conn).schemas_supported:
        cur = conn.cursor()
        cur.execute(f"SET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM")


def _check_values(info: dict[str, Any], values: dict[str, str | None]) -> dict[str, str | None]:
    names = [p["name"] for p in info["inputs"]]
    unknown = [k for k in values if k not in names]
    if unknown:
        raise RunError("Unknown input parameter(s): " + ", ".join(unknown))
    return {n: values.get(n) for n in names}


def call_sql(info: dict[str, Any], values: dict[str, str | None]) -> str:
    """SQL that calls the routine itself with the given input values."""
    args = ", ".join(psql.literal(values.get(p["name"])) for p in info["inputs"])
    name = quote_ident(info["name"])
    if info["kind"] == "function":
        return f"SELECT {name}({args}) AS RESULT FROM RDB$DATABASE"
    if info["kind"] == "selectable":
        return f"SELECT * FROM {name}" + (f"({args})" if args else "")
    return f"EXECUTE PROCEDURE {name}" + (f"({args})" if args else "")


def _fetch(conn, sql: str, limit: int, params: list | None = None,
           batch: int = 1) -> tuple[list[tuple], list[str], str | None, bool]:
    """Execute ``sql`` and fetch at most ``limit`` rows, ``batch`` rows per round trip.

    With ``batch=1`` rows sent before an error are not lost (the driver drops a whole batch when the
    server reports an error in the middle of it).

    Returns (rows, column names, error message, stopped at ``limit``).
    """
    cur = conn.cursor()
    if not hasattr(cur, "transaction"):  # native driver (fbadmin.native): plain cursor fetches
        return _fetch_cursor(cur, sql, limit, params)
    cur.transaction.check_trans_handle()
    stmt = cur._get_stmt(sql)  # compile errors are raised here
    names = [x.aliasname for x in stmt.xsqlda]
    cn = cur.transaction.connection
    rows: list[tuple] = []
    error = None
    stopped = False
    try:
        if stmt.stmt_type == fbcore.isc_info_sql_stmt_select:
            cn._op_execute(stmt.handle, cur.transaction.trans_handle, cur._convert_params(params or []))
            cn._op_response()
            blr = fbcore.calc_blr(stmt.xsqlda)
            while True:
                if len(rows) >= limit:
                    stopped = True
                    break
                cn._op_fetch(stmt.handle, blr, max(1, min(batch, limit - len(rows))))
                got, more = cn._op_fetch_response(stmt.handle, stmt.xsqlda)
                rows.extend(stmt.fetch_generator(got, False, 1))
                if not more:
                    break
        else:  # EXECUTE PROCEDURE: one row of outputs
            cur.execute(sql, params or [])
            r = cur.fetchone() if cur.description else None
            rows = [tuple(r)] if r is not None else []
    except Exception as exc:  # noqa: BLE001 - the error is part of the result
        error = error_message(exc)
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
    return rows, names, error, stopped


def _fetch_cursor(cur, sql: str, limit: int, params: list | None) -> tuple[list[tuple], list[str], str | None, bool]:
    """:func:`_fetch` for the native driver: libfbclient batches the fetches itself and hands
    out the rows the server sent before an error, then raises the error."""
    stmt = cur.prepare(sql)  # compile errors are raised here, like in _fetch
    rows: list[tuple] = []
    names: list[str] = []
    error = None
    stopped = False
    try:
        cur.execute(stmt, params or [])
        names = [d[0] for d in cur.description or ()]
        while cur.description:
            if len(rows) >= limit:
                stopped = True
                break
            r = cur.fetchone()
            if r is None:
                break
            rows.append(tuple(r))
    except Exception as exc:  # noqa: BLE001 - the error is part of the result
        error = error_message(exc)
    finally:
        try:
            cur.close()
            stmt.free()
        except Exception:  # noqa: BLE001
            pass
    return rows, names, error, stopped


def _short(v: Any, limit: int = VALUE_CHARS) -> Any:
    v = to_json(v)
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + "…"
    return v


def _describe_types(conn, tvars: list[TraceVar]) -> None:
    """Pick how each traced variable is copied into its trace column: BLOBs as their size, very long
    strings as their first characters (one trace row must stay below Firebird's 64 KB message limit)."""
    traced = [v for v in tvars if v.column]
    if not traced:
        return
    sql = "SELECT " + ", ".join(f"CAST(NULL AS {psql.bare_type(v.type)}) AS C{i}" for i, v in enumerate(traced)) \
        + " FROM RDB$DATABASE"
    try:
        ps = conn.cursor().prep(sql)
    except Exception:  # noqa: BLE001 - the instrumented block will report a useful error itself
        return
    xs = list(ps.stmt.xsqlda)
    ps.stmt.drop()  # (close() would try to close a cursor that was never opened)
    charsets = {r["id"]: r["name"] for r in Catalog(conn).rows(
        "SELECT RDB$CHARACTER_SET_ID AS id, TRIM(RDB$CHARACTER_SET_NAME) AS name FROM RDB$CHARACTER_SETS", scoped=False)}
    strings = []
    for v, x in zip(traced, xs, strict=True):
        base = x.sqltype & ~1
        if base == _SQL_TYPE_BLOB:
            v.mode, v.col_type = "size", "BIGINT"
        elif base in (_SQL_TYPE_TEXT, _SQL_TYPE_VARYING):
            strings.append((v, x))
    total = sum(x.sqllen for _v, x in strings)
    for v, x in strings:
        if x.sqllen > LONG_STRING_BYTES or (total > MESSAGE_BUDGET and x.sqllen > 400):
            cs = charsets.get(x.sqlsubtype & 0xFF, "UTF8")
            chars = TEXT_CHARS if total <= MESSAGE_BUDGET else 100
            v.mode, v.text_chars = "text", chars
            v.col_type = f"VARCHAR({chars}) CHARACTER SET {quote_ident(cs)}"


def _render(v: TraceVar, value: Any) -> Any:
    if value is None:
        return None
    if v.mode == "size":
        return f"<BLOB, {value} bytes>"
    if v.mode == "text" and isinstance(value, str) and len(value) >= v.text_chars:
        return value[:VALUE_CHARS] + "…"
    return _short(value)


def map_error(message: str | None) -> tuple[str | None, int | None]:
    """Error text with ``At block line: N`` (the instrumented block) shown as a source line."""
    if not message:
        return message, None
    lines = [int(m.group(1)) for m in _BLOCK_LINE.finditer(message)]
    text = _BLOCK_LINE.sub(lambda m: f"At source line {m.group(1)}", message)
    return text, (lines[-1] if lines else None)


# ------------------------------------------------------------------ trace run
def trace(conn, info: dict[str, Any], values: dict[str, str | None], *, watch: list[str] | None = None,
          max_steps: int = MAX_STEPS, block_sql: str | None = None) -> dict[str, Any]:
    """Run the instrumented copy of the routine (or block) and return the recorded steps.

    ``conn`` must be a dedicated attachment: everything happens in one transaction that is rolled back.
    """
    if not info.get("traceable"):
        raise RunError(info.get("reason") or "This routine cannot be traced")
    max_steps = max(1, min(int(max_steps), MAX_STEPS_LIMIT))
    values = _check_values(info, values)
    _schema(conn)
    if block_sql is not None:
        parsed = psql.parse_execute_block(block_sql)
    else:
        parsed = psql.parse_source(info["source"] or "")
    inputs, outputs = _vars(info, "inputs"), _vars(info, "outputs")
    tvars = psql.default_trace_vars(parsed, inputs, set(watch) if watch is not None else None)
    _describe_types(conn, tvars)
    cat = Catalog(conn)
    major = cat.engine_major
    # Firebird 4+: errors escaping the routine come back as a row (RDB$ERROR), so rows can be fetched
    # in batches; Firebird 3 has no RDB$ERROR: fetch one row per round trip instead (slower)
    fast = major >= 4
    ins = psql.instrument(parsed, kind=info["kind"], inputs=inputs, outputs=outputs, values=values,
                          returns=info.get("returns"), tvars=tvars, with_message=fast, catch_errors=fast)
    if major < 4 and len(ins.sql.encode("utf-8")) > FB3_SQL_LIMIT:
        raise RunError("The instrumented block is longer than the 64 KB Firebird 3 allows for one statement: "
                       "watch fewer variables.")
    t0 = time.perf_counter()
    try:
        try:
            rows, _names, error, stopped = _fetch(conn, ins.sql, max_steps + 1, batch=400 if fast else 1)
        except Exception as exc:  # noqa: BLE001 - compile error of the instrumented block
            msg = error_message(exc)
            m = re.search(r"line (\d+), column (\d+)", msg)
            return {"compiled": False, "error": {"message": msg, "line": int(m.group(1)) if m else None}, "instrumented_sql": ins.sql,
                    "warnings": ins.warnings, "steps": [], "variables": [], "rows": [], "columns": [],
                    "result": None, "stopped": False, "elapsed_ms": 0, "rolled_back": True}
    finally:
        end_transaction(conn, commit=False)
    elapsed = round((time.perf_counter() - t0) * 1000, 2)
    return _steps(ins, rows, error, stopped, max_steps, elapsed)


def _steps(ins: psql.Instrumented, rows: list[tuple], error: str | None, stopped: bool, max_steps: int,
           elapsed: float) -> dict[str, Any]:
    roles = ins.columns
    out_idx = [i for i, (role, _n) in enumerate(roles) if role == "output"]
    res_idx = next((i for i, (role, _n) in enumerate(roles) if role == "result"), None)
    var_idx = [i for i, (role, _n) in enumerate(roles) if role == "var"]
    pos = {name: i for i, (role, name) in enumerate(roles)}
    variables: list[dict[str, Any]] = [{"name": v.name, "kind": v.kind, "type": v.type, "mode": v.mode}
                                       for v in ins.variables]
    variables += [{"name": o.name, "kind": "output", "type": o.decl, "mode": "value"} for o in ins.outputs]
    if res_idx is not None:
        variables.append({"name": "RESULT", "kind": "result", "type": "", "mode": "value"})
    value_idx = var_idx + out_idx + ([res_idx] if res_idx is not None else [])
    tv = ins.variables
    steps: list[dict[str, Any]] = []
    out_rows: list[list[Any]] = []
    final = None
    prev: list[Any] | None = None
    for r in rows[:max_steps]:
        k, line = r[0], r[1]
        vals = [_render(tv[j], r[i]) for j, i in enumerate(var_idx)]
        vals += [_short(r[i]) for i in out_idx]
        if res_idx is not None:
            vals.append(_short(r[res_idx]))
        step: dict[str, Any] = {"k": psql.KIND_NAMES.get(k, str(k)), "line": line, "v": vals}
        if prev is not None:
            step["changed"] = [j for j, (a, b) in enumerate(zip(prev, vals, strict=True)) if a != b]
        prev = vals
        if k == psql.ROW and len(out_rows) < MAX_ROWS:
            out_rows.append([to_json(r[i]) for i in out_idx])
            step["row"] = len(out_rows)
        elif k == psql.HANDLER:
            step["caught"] = {"gdscode": r[pos["FBT$G"]], "sqlstate": r[pos["FBT$S"]],
                              "message": r[pos["FBT$M"]] if "FBT$M" in pos else None}
        elif k == psql.FINAL:
            final = r
        elif k == psql.ERROR:  # an error escaping the routine, caught by the instrumented block
            error = error or r[pos["FBT$M"]] or f"GDSCODE {r[pos['FBT$G']]}, SQLSTATE {r[pos['FBT$S']]}"
            continue
        if step.get("caught") and step["caught"]["message"]:
            step["caught"]["message"] = map_error(step["caught"]["message"])[0]
        steps.append(step)
    result = None
    if final is not None and not error:
        if ins.kind == "executable" and out_idx:
            out_rows = [[to_json(final[i]) for i in out_idx]]
        elif ins.kind == "function":
            result = to_json(final[res_idx])
    msg, line = map_error(error)
    return {
        "compiled": True,
        "variables": variables,
        "value_count": len(value_idx),
        "steps": steps,
        "stopped": stopped or len(rows) > max_steps,
        "max_steps": max_steps,
        "columns": [o.name for o in ins.outputs] if ins.kind != "function" else ["RESULT"],
        "rows": out_rows if ins.kind != "function" else ([[result]] if final is not None and not error else []),
        "result": result,
        "error": {"message": msg, "line": line, "last_line": steps[-1]["line"] if steps else None} if error else None,
        "warnings": ins.warnings,
        "instrumented_sql": ins.sql,
        "elapsed_ms": elapsed,
        "rolled_back": True,
    }


# ------------------------------------------------------------------ plain runs, profiler
def run_call(conn, info: dict[str, Any], values: dict[str, str | None], *, block_sql: str | None = None,
             max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Call the routine itself (not instrumented); the caller rolls back."""
    values = _check_values(info, values)
    params: list = []
    if block_sql is not None:
        sql = block_sql.rstrip().rstrip(";")
        params = [values[p["name"]] for p in info["inputs"]]
    else:
        sql = call_sql(info, values)
    t0 = time.perf_counter()
    try:
        rows, names, error, stopped = _fetch(conn, sql, max_rows, params)
    except Exception as exc:  # noqa: BLE001 - compile error
        rows, names, error, stopped = [], [], error_message(exc), False
    return {"sql": sql, "columns": [n.strip() for n in names], "rows": [[to_json(v) for v in r] for r in rows],
            "error": error, "truncated": stopped, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}


def profile(conn, info: dict[str, Any], values: dict[str, str | None], *, block_sql: str | None = None) -> dict:
    """Run the routine itself under the Firebird 5 profiler (rolled back) and return per-line stats
    mapped to the stored source lines."""
    from ..monitoring import profiler

    if not profiler.available(conn):
        return {"available": False,
                "reason": "Per-line timings need the Firebird 5 profiler (RDB$PROFILER); this server runs "
                          f"Firebird {Catalog(conn).engine_version}."}
    _schema(conn)
    values = _check_values(info, values)
    cur = conn.cursor()
    cur.execute(f"SELECT RDB$PROFILER.START_SESSION(?, NULL, NULL, NULL, '{profiler.OPTIONS}') FROM RDB$DATABASE",
                [f"fbadmin debugger: {info['name'] or 'EXECUTE BLOCK'}"[:255]])
    profile_id = cur.fetchone()[0]
    try:
        run = run_call(conn, info, values, block_sql=block_sql)
    finally:
        cur = conn.cursor()
        cur.execute("EXECUTE PROCEDURE RDB$PROFILER.FINISH_SESSION(TRUE)")
        end_transaction(conn, commit=False)  # undo the routine's writes; the profile data was flushed
    rep = profiler.report(conn, profile_id)
    end_transaction(conn, commit=True)
    if info["otype"] == "block":
        lines = [ln for ln in rep["lines"] if ln["statement_type"] == "BLOCK" and not ln["routine_name"]]
        offset = 0
    else:
        stype = "PROCEDURE" if info["otype"] == "procedures" else "FUNCTION"
        lines = [ln for ln in rep["lines"] if ln["statement_type"] == stype and ln["routine_name"] == info["name"]
                 and not ln["package_name"]]
        src = next((r for r in rep["routines"] if r["statement_type"] == stype and r["routine_name"] == info["name"]
                    and not r["package_name"]), None)
        offset = src["line_offset"] if src else 0
    by_line: dict[int, dict[str, Any]] = {}
    for ln in lines:
        k = (ln["line_num"] or 0) - offset
        e = by_line.setdefault(k, {"line": k, "count": 0, "total_ns": 0, "max_ns": 0})
        e["count"] += ln["counter"] or 0
        e["total_ns"] += int(ln["total_ns"] or 0)
        e["max_ns"] = max(e["max_ns"], int(ln["max_ns"] or 0))
    return to_json({"available": True, "profile_id": profile_id, "line_offset": offset,
                    "lines": sorted(by_line.values(), key=lambda e: e["line"]), "total_ns": rep["total_ns"],
                    "run": run})


# ------------------------------------------------------------------ tests
def _same(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    e, a = str(expected).rstrip(), str(actual).rstrip()
    if e == a:
        return True
    if e.lower() in ("true", "false") and a.lower() in ("true", "false"):
        return e.lower() == a.lower()
    try:
        return decimal.Decimal(e) == decimal.Decimal(a)
    except (decimal.InvalidOperation, ValueError):
        pass
    # timestamps: "2024-01-02 10:00:00" vs ISO "2024-01-02T10:00:00"
    return e.replace(" ", "T") == a.replace(" ", "T")


def check(expect: dict[str, Any], run: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compare a run with the expected outcome; return (passed, differences)."""
    diffs: list[str] = []
    want_error = expect.get("error")
    if want_error is not None:
        if not run["error"]:
            return False, [f"expected an error containing {want_error!r}, but the call succeeded"]
        if want_error and want_error.lower() not in run["error"].lower():
            return False, [f"expected an error containing {want_error!r}, got: {run['error']}"]
        return True, []
    if run["error"]:
        return False, [f"unexpected error: {run['error']}"]
    rows = expect.get("rows")
    if rows is None:
        return True, []
    actual = run["rows"]
    if len(rows) != len(actual):
        diffs.append(f"expected {len(rows)} row(s), got {len(actual)}")
    for i, (er, ar) in enumerate(zip(rows, actual, strict=False)):
        if len(er) != len(ar):
            diffs.append(f"row {i + 1}: expected {len(er)} value(s), got {len(ar)}")
            continue
        for j, (ev, av) in enumerate(zip(er, ar, strict=True)):
            if not _same(ev, av):
                col = run["columns"][j] if j < len(run["columns"]) else str(j + 1)
                diffs.append(f"row {i + 1}, {col}: expected {ev!r}, got {av!r}")
    return not diffs, diffs


def run_test(conn, info: dict[str, Any], test: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        run = run_call(conn, info, test.get("inputs") or {})
    except RunError as exc:
        return {"id": test["id"], "status": "error", "diffs": [str(exc)], "run": None, "elapsed_ms": 0}
    finally:
        end_transaction(conn, commit=False)
    ok, diffs = check(test.get("expect") or {}, run)
    return {"id": test["id"], "status": "pass" if ok else "fail", "diffs": diffs, "run": run,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}

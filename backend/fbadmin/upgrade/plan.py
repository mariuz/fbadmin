"""The migration plan for an upgrade report, and the report rendered as Markdown or HTML."""

from __future__ import annotations

import html
import re
from typing import Any

from . import keywords as K

ODS_TABLE = [("3.0", "12.0"), ("4.0", "13.0"), ("5.0", "13.1"), ("6.0", "14.0")]


def _has(report: dict, fid: str) -> dict | None:
    return next((f for f in report.get("findings", []) if f["id"] == fid), None)


def _size(n: int | None) -> str:
    if not n:
        return "unknown size"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _base(path: str) -> str:
    name = re.split(r"[\\/]", path or "database")[-1]
    return re.sub(r"\.[A-Za-z0-9]+$", "", name) or "database"


def settings_rows(report: dict) -> list[list[str]]:
    """Server settings to review: [setting, why, recommendation, value on the trial target]."""
    src, tgt = report["source"]["version"], report["target"]["version"]
    crosses4 = float(src) < 4 <= float(tgt)
    cfg = ((report.get("trial") or {}).get("config")) or {}
    rows: list[tuple[str, str, str]] = []
    if _has(report, "legacy_udfs") or _has(report, "blob_filters"):
        rows.append(("UdfAccess", "the database declares legacy UDFs or BLOB filters",
                     "None (after replacing the UDFs) or Restrict <dir>"))
    if _has(report, "external_tables"):
        rows.append(("ExternalFileAccess", "the database has external tables", "Restrict <dir with the files>"))
    rows.append(("AuthServer / UserManager", "users are kept per server; FB4+ default is Srp256 / Srp",
                 "Srp256 (add Srp only for old clients)" if crosses4 else "as on the old server"))
    rows.append(("WireCrypt", "network encryption (default Required)", "as on the old server"))
    if crosses4:
        rows.append(("DataTypeCompatibility", "clients that do not know INT128, DECFLOAT, WITH TIME ZONE",
                     "3.0 until every driver is upgraded"))
        rows.append(("ReadConsistency", "READ COMMITTED behaviour changed in FB4", "1 (default); 0 only while testing"))
        rows.append(("DefaultTimeZone", "session time zone for the new WITH TIME ZONE types", "the server's zone"))
    rows.append(("DefaultDbCachePages", "counted in pages, so the memory depends on the page size",
                 "same memory as before: old pages × old page size / new page size"))
    rows.append(("ServerMode", "SuperServer / SuperClassic / Classic", "as on the old server"))
    if float(tgt) >= 5:
        rows.append(("ParallelWorkers / MaxParallelWorkers", "FB5+: parallel restore, sweep and index creation",
                     "e.g. MaxParallelWorkers = number of cores; then gbak -par N"))
    out = []
    for name, why, rec in rows:
        vals = [f"{k}={cfg[k]['value']}" for k in name.replace(" ", "").split("/") if k in cfg]
        out.append([name, why, rec, ", ".join(vals) if vals else ""])
    return out


def build_plan(report: dict) -> list[dict[str, Any]]:
    src = report["source"]
    tgt = report["target"]
    sv, tv = src["version"], tgt["version"]
    db = report.get("database") or {}
    host = db.get("host") or "old-host"
    port = db.get("port") or 3050
    path = db.get("path") or src.get("file") or "/path/to/db.fdb"
    tsrv = report.get("target_server") or {}
    thost, tport = tsrv.get("host") or "new-host", tsrv.get("port") or 3050
    newpath = db.get("new_path") or re.sub(r"(\.[A-Za-z0-9]+)?$", f"_fb{tv[0]}\\1", path, count=1)
    fbk = f"{_base(path)}_fb{sv[0]}.fbk"
    counts = report.get("counts") or {}
    page = src.get("page_size") or 0
    page_opt = " -page_size 8192" if float(tv) >= 6 and page < 8192 else ""
    par = " -par 4" if float(tv) >= 5 else ""
    old_db, new_db = f"{host}/{port}:{path}", f"{thost}/{tport}:{newpath}"
    steps: list[dict[str, Any]] = []

    def step(title: str, text: list[str], commands: list[str] | None = None, table: dict | None = None):
        steps.append({"title": title, "text": text, "commands": commands or [], "table": table})

    blockers = counts.get("blocker", 0)
    step("Fix what the checks found, on the old server", [
        f"Resolve the {blockers} blocker(s) and review the {counts.get('warning', 0)} warning(s) in this report. "
        "Most fixes (quoting or renaming reserved words, replacing UDF calls with built-in functions, "
        "LOCALTIMESTAMP instead of CURRENT_TIMESTAMP) work on Firebird " + sv + " already, so they can be "
        "deployed and tested before the upgrade." if blockers else
        f"No blockers were found. Review the {counts.get('warning', 0)} warning(s) in this report.",
        "Freeze schema changes until the migration is finished, and search the application code for the "
        "reserved words listed in this report: SQL written in the applications is not checked here.",
    ])
    step(f"Install Firebird {tv} next to the old server", [
        f"Install Firebird {tv} on a new host, or on the same host with another port, and configure "
        "firebird.conf / databases.conf. Review these settings:",
    ], table={"columns": ["Setting", "Why", "Recommendation", "Trial target value"], "rows": settings_rows(report)})
    step("Move the users", [
        "Users are stored in the server's security database, not in the database itself, so they do not travel "
        "with a backup. Recreate them on the new server (CREATE USER ... PASSWORD ... USING PLUGIN Srp), or "
        f"migrate the security database with gbak as described in the Firebird {tv} release notes. "
        "Re-check SYSDBA's password and the RDB$ADMIN role memberships.",
    ])
    step("Rehearse with a trial migration", [
        "Run the trial restore of this assistant (or the commands of the next steps without shutting the "
        "database down) and keep the restored copy. Point a test instance of each application at it."
        + (" The last trial restore of this report succeeded." if report.get("trial") else ""),
        "Time the backup and the restore: they give the length of the maintenance window "
        f"(the database is {_size(src.get('size_bytes'))}).",
    ])
    step("Stop the applications and shut the old database down", [
        "Announce the window, stop the applications, then take the database to full shutdown so nothing "
        "changes after the backup:",
    ], [f"gfix -shut full -force 0 -user SYSDBA -password ******** {old_db}"])
    step(f"Back up with the Firebird {sv} gbak", [
        f"Use the gbak of the old Firebird {sv} installation. -g skips garbage collection (faster), -v lists "
        "progress. Do not use nbackup or copy the file: nbackup copies pages in the old ODS, which the new "
        "server cannot open.",
    ], [f"gbak -b -g -v -user SYSDBA -password ******** {old_db} {fbk}"])
    step(f"Restore with the Firebird {tv} gbak", [
        f"Use the gbak of the new Firebird {tv} installation. The restore writes ODS {tgt['ods']}."
        + (" Firebird 6 needs a page size of at least 8192." if page_opt else "")
        + (" -par uses parallel workers (Firebird 5+; MaxParallelWorkers must allow it)." if par else ""),
        "Or stream the backup straight into the restore without an intermediate file:",
    ], [f"gbak -c -v{page_opt}{par} -user SYSDBA -password ******** {fbk} {new_db}",
        f"<fb{sv[0]}>/bin/gbak -b -g -user SYSDBA -password ******** {old_db} stdout | "
        f"<fb{tv[0]}>/bin/gbak -c{page_opt}{par} -user SYSDBA -password ******** stdin {new_db}"])
    fixes = []
    if _has(report, "legacy_udfs"):
        fixes.append("Replace the legacy UDFs (see the finding) or run misc/upgrade/v4.0/udf_replace.sql from the "
                     "new installation against the restored database.")
    if _has(report, "trial_recompile") or _has(report, "reserved_in_source"):
        fixes.append("Recompile the objects listed under reserved words once their sources are fixed.")
    step("Check the restored database", [
        f"Connect with isql and confirm the ODS ({tgt['ods']}), the dialect ({src.get('dialect')}) and the page "
        "size; set forced writes and the sweep interval as before (gbak does not keep every header setting).",
        *fixes,
        "Compare the old and the new database with fbadmin's Schema diff, and compare row counts.",
    ], ["SELECT MON$ODS_MAJOR, MON$ODS_MINOR, MON$PAGE_SIZE, MON$SQL_DIALECT FROM MON$DATABASE;",
        f"gfix -write sync -user SYSDBA -password ******** {new_db}",
        _row_count_sql(report)])
    fb4 = "; timestamps (time zone types); concurrent updates (read consistency)" if float(sv) < 4 <= float(tv) else ""
    step("Test before switching", [
        "Run the applications' test suite against the new database. In particular: logins of every user and "
        f"role; every report and batch job{fb4}; performance of the heaviest queries (the optimizer changes with "
        "each version; compare plans and timings with the old server).",
        "Update fbclient and the drivers on the application hosts to the new version.",
    ])
    step("Switch over", [
        "Change the applications' connection strings (or the databases.conf alias) to the new server and "
        "start them. Keep the old server installed, with the old database in shutdown, until the new one has run "
        "a full business cycle.",
    ])
    step("Rollback plan", [
        "Until the switch: bring the old database back online and restart the applications against it; "
        "nothing was changed there.",
        "After the switch: a backup made by Firebird " + tv + " cannot be restored on Firebird " + sv + ". "
        "Changes written to the new database must be copied back with SQL (for example fbadmin's Copy data, "
        "or a replication tool), so decide in advance how long the rollback window is.",
    ], [f"gfix -online normal -user SYSDBA -password ******** {old_db}"])
    return steps


def _row_count_sql(report: dict) -> str:
    tables = (report.get("source") or {}).get("tables") or []
    if not tables:
        return "-- SELECT COUNT(*) FROM <table>; for each table, on both servers"
    parts = [f"SELECT '{t.replace(chr(39), chr(39) * 2)}' AS tbl, COUNT(*) AS cnt FROM {K.qi(t)}" for t in tables[:50]]
    more = f"\n-- ... and {len(tables) - 50} more tables" if len(tables) > 50 else ""
    return "\nUNION ALL ".join(parts) + ";" + more


# ================================================================== rendering
SEV_LABEL = {"blocker": "Blocker", "warning": "Warning", "info": "Info"}


def _obj_line(o: dict) -> str:
    s = f"{o['type']} {o['name']}"
    if o.get("parent") and o["type"] not in ("column",):
        s += f" ({o['parent']})"
    elif o.get("parent"):
        s = f"column {o['parent']}.{o['name']}"
    if o.get("detail"):
        s += f" — {o['detail']}"
    if o.get("fix"):
        s += f" — fix: {o['fix']}"
    return s


def to_markdown(report: dict) -> str:
    src, tgt = report["source"], report["target"]
    db = report.get("database") or {}
    c = report.get("counts") or {}
    out = [f"# Upgrade report: {db.get('name') or 'database'} → Firebird {tgt['version']}", ""]
    out += [f"Generated by fbadmin on {report.get('generated_at', '')}.", ""]
    out += ["| | Source | Target |", "|---|---|---|",
            f"| Firebird | {src.get('engine_version')} | {tgt['version']} |",
            f"| ODS | {src.get('ods')} | {tgt['ods']} |",
            f"| Page size | {src.get('page_size')} | |",
            f"| Dialect | {src.get('dialect')} | |",
            f"| Size | {_size(src.get('size_bytes'))} | |", ""]
    out += [f"**{c.get('blocker', 0)} blocker(s), {c.get('warning', 0)} warning(s), {c.get('info', 0)} info.**", ""]
    out += ["## Findings", ""]
    for f in report.get("findings", []):
        out += [f"### [{SEV_LABEL[f['severity']]}] {f['title']}", "", f"*{f['category']}*", "", f["explanation"], "",
                f"**Fix:** {f['fix']}", ""]
        for o in f.get("objects", [])[:500]:
            out.append(f"- {_obj_line(o)}")
        if f.get("objects"):
            out.append("")
        for r in f.get("refs") or []:
            if r:
                out += [f"See: {r}", ""]
    out += ["## ODS per Firebird version", "", "| Firebird | ODS |", "|---|---|"]
    out += [f"| {v} | {o} |" for v, o in ODS_TABLE] + [""]
    out += ["## Migration plan", ""]
    for i, s in enumerate(report.get("plan") or [], 1):
        out += [f"### {i}. {s['title']}", ""]
        for p in s["text"]:
            out += [p, ""]
        if s.get("table"):
            t = s["table"]
            out.append("| " + " | ".join(t["columns"]) + " |")
            out.append("|" + "---|" * len(t["columns"]))
            out += ["| " + " | ".join(r) + " |" for r in t["rows"]]
            out.append("")
        if s.get("commands"):
            out += ["```"] + s["commands"] + ["```", ""]
    ks = report.get("keyword_sources") or {}
    if ks:
        out += ["## Sources of the keyword lists", ""] + [f"- Firebird {v}: {s}" for v, s in ks.items()] + [""]
    return "\n".join(out)


_CSS = """body{font:14px/1.5 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1f2937}
h1{font-size:22px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #e5e7eb}h3{font-size:15px;margin-bottom:4px}
table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #d1d5db;padding:4px 8px;text-align:left;vertical-align:top}
pre{background:#f3f4f6;padding:8px;overflow-x:auto;white-space:pre-wrap}.sev{display:inline-block;padding:0 6px;border-radius:4px;color:#fff;font-size:12px}
.blocker{background:#b91c1c}.warning{background:#b45309}.info{background:#1d4ed8}.cat{color:#6b7280}ul{margin-top:4px}"""


def to_html(report: dict) -> str:
    e = html.escape
    src, tgt = report["source"], report["target"]
    db = report.get("database") or {}
    c = report.get("counts") or {}
    title = f"Upgrade report: {db.get('name') or 'database'} → Firebird {tgt['version']}"
    h = [f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{e(title)}</title>"
         f"<style>{_CSS}</style></head><body>", f"<h1>{e(title)}</h1>",
         f"<p>Generated by fbadmin on {e(str(report.get('generated_at', '')))}.</p>",
         "<table><tr><th></th><th>Source</th><th>Target</th></tr>",
         f"<tr><td>Firebird</td><td>{e(str(src.get('engine_version')))}</td><td>{e(tgt['version'])}</td></tr>",
         f"<tr><td>ODS</td><td>{e(str(src.get('ods')))}</td><td>{e(tgt['ods'])}</td></tr>",
         f"<tr><td>Page size</td><td>{e(str(src.get('page_size')))}</td><td></td></tr>",
         f"<tr><td>Dialect</td><td>{e(str(src.get('dialect')))}</td><td></td></tr>",
         f"<tr><td>Size</td><td>{e(_size(src.get('size_bytes')))}</td><td></td></tr></table>",
         f"<p><b>{c.get('blocker', 0)} blocker(s), {c.get('warning', 0)} warning(s), {c.get('info', 0)} info.</b></p>",
         "<h2>Findings</h2>"]
    for f in report.get("findings", []):
        h.append(f"<h3><span class=\"sev {f['severity']}\">{SEV_LABEL[f['severity']]}</span> {e(f['title'])}</h3>")
        h.append(f"<div class=\"cat\">{e(f['category'])}</div><p>{e(f['explanation'])}</p>")
        h.append(f"<p><b>Fix:</b> {e(f['fix'])}</p>")
        if f.get("objects"):
            h.append("<ul>" + "".join(f"<li>{e(_obj_line(o))}</li>" for o in f["objects"][:500]) + "</ul>")
        for r in f.get("refs") or []:
            if r:
                h.append(f"<p>See: <a href=\"{e(r)}\">{e(r)}</a></p>")
    h.append("<h2>ODS per Firebird version</h2><table><tr><th>Firebird</th><th>ODS</th></tr>"
             + "".join(f"<tr><td>{v}</td><td>{o}</td></tr>" for v, o in ODS_TABLE) + "</table>")
    h.append("<h2>Migration plan</h2>")
    for i, s in enumerate(report.get("plan") or [], 1):
        h.append(f"<h3>{i}. {e(s['title'])}</h3>")
        h += [f"<p>{e(p)}</p>" for p in s["text"]]
        if s.get("table"):
            t = s["table"]
            h.append("<table><tr>" + "".join(f"<th>{e(x)}</th>" for x in t["columns"]) + "</tr>"
                     + "".join("<tr>" + "".join(f"<td>{e(x)}</td>" for x in r) + "</tr>" for r in t["rows"])
                     + "</table>")
        if s.get("commands"):
            h.append("<pre>" + e("\n".join(s["commands"])) + "</pre>")
    ks = report.get("keyword_sources") or {}
    if ks:
        h.append("<h2>Sources of the keyword lists</h2><ul>"
                 + "".join(f"<li>Firebird {e(v)}: {e(s)}</li>" for v, s in ks.items()) + "</ul>")
    h.append("</body></html>")
    return "\n".join(h)

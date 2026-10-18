"""Copy table data between databases (on the same or different servers), as a background job."""

from __future__ import annotations

from ..metadata import Catalog
from ..metadata.types import quote_ident
from .model import column_fragment, load_collations, strip_comment_lines, topo_sort


def table_ddl(cat: Catalog, name: str, computed: bool = True) -> str:
    """CREATE TABLE with columns, primary and unique keys (no foreign keys, checks or triggers)."""
    r = cat.one("SELECT RDB$RELATION_TYPE AS rt FROM RDB$RELATIONS WHERE RDB$RELATION_NAME = ?", (name,)) or {}
    cols = cat.columns(name)
    lines = ["    " + column_fragment(c) for c in cols if computed or not c["computed"]]
    lines += ["    " + cat._constraint_ddl(c) for c in cat.constraints(name) if c["type"] in ("PRIMARY KEY", "UNIQUE")]
    head = "CREATE GLOBAL TEMPORARY TABLE" if r.get("rt") in (4, 5) else "CREATE TABLE"
    s = f"{head} {quote_ident(name)} (\n" + ",\n".join(lines) + "\n)"
    if r.get("rt") == 4:
        s += " ON COMMIT PRESERVE ROWS"
    elif r.get("rt") == 5:
        s += " ON COMMIT DELETE ROWS"
    return s


def copy_tables(src, dst, log, *, tables: list[str], create_tables: bool = True, truncate: bool = False,
                batch_size: int = 1000, result: dict | None = None) -> dict:
    result = result if result is not None else {}
    result.update(tables={}, rows=0)
    scat, dcat = Catalog(src), Catalog(dst)
    existing = {t["name"] for t in dcat.list_objects("tables")}
    src_tables = {t["name"] for t in scat.list_objects("tables")}
    missing = [t for t in tables if t not in src_tables]
    if missing:
        raise LookupError("Not found in the source database: " + ", ".join(missing))
    # parents first, so foreign keys in the target are satisfied
    deps = {t: {c["ref_table"] for c in scat.constraints(t) if c["type"] == "FOREIGN KEY"} - {t} for t in tables}
    order = topo_sort(sorted(tables), deps)
    dcur = dst.cursor()
    created = []
    if create_tables:
        dom_existing = {d["name"] for d in dcat.list_objects("domains")}
        src_coll = load_collations(scat)
        coll_existing = set(load_collations(dcat))
        for t in order:
            if t in existing:
                continue
            for c in scat.columns(t):
                if c["collation"] in src_coll and c["collation"] not in coll_existing:
                    log(src_coll[c["collation"]]["ddl"])
                    dcur.execute(src_coll[c["collation"]]["ddl"])
                    coll_existing.add(c["collation"])
                if c["domain"] and c["domain"] not in dom_existing:
                    ddl = strip_comment_lines(scat._get_domains(c["domain"])["ddl"])
                    log(ddl)
                    dcur.execute(ddl.rstrip(";"))
                    dom_existing.add(c["domain"])
            dst.commit()  # domains and collations survive a failed CREATE TABLE below
            ddl = table_ddl(scat, t)
            try:
                dcur.execute(ddl)
                dst.commit()
            except Exception as exc:  # noqa: BLE001 - computed columns may use objects the target lacks
                dst.rollback()
                log(f"{t}: {str(exc).strip().splitlines()[0]} - creating it without computed columns")
                ddl = table_ddl(scat, t, computed=False)
                dcur.execute(ddl)
                dst.commit()
            log(f"Created table {t}")
            created.append(t)
            existing.add(t)
    for t in order:
        if t not in existing:
            raise LookupError(f"Table {t} does not exist in the target database (enable 'create tables')")
    if truncate:
        for t in reversed(order):
            if t not in created:
                dcur.execute(f"DELETE FROM {quote_ident(t)}")
                log(f"{t}: deleted {dcur.rowcount} existing row(s)")
        dst.commit()

    scur = src.cursor()
    for t in order:
        s_cols = {c["name"]: c for c in scat.columns(t) if not c["computed"]}
        d_meta = dcat.columns(t)
        d_cols = [c for c in d_meta if not c["computed"] and c["name"] in s_cols]
        if not d_cols:
            log(f"{t}: no common columns, skipped")
            continue
        names = ", ".join(quote_ident(c["name"]) for c in d_cols)
        overriding = any(c["identity"] == "ALWAYS" for c in d_cols) and dcat.engine_major >= 4
        sql = (f"INSERT INTO {quote_ident(t)} ({names}) " + ("OVERRIDING SYSTEM VALUE " if overriding else "")
               + "VALUES (" + ", ".join("?" for _ in d_cols) + ")")
        stmt = dcur.prep(sql)
        scur.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}")
        total = scur.fetchone()[0]
        scur.execute(f"SELECT {names} FROM {quote_ident(t)}")
        n = 0
        while True:
            rows = scur.fetchmany(batch_size)
            if not rows:
                break
            for r in rows:
                dcur.execute(stmt, list(r))
            n += len(rows)
            dst.commit()
            log(f"{t}: {n} / {total} rows")
        result["tables"][t] = n
        result["rows"] += n
        # move identity columns past the copied values
        for c in d_cols:
            if c["identity"] and n:
                dcur.execute(f"SELECT MAX({quote_ident(c['name'])}) FROM {quote_ident(t)}")
                mx = dcur.fetchone()[0]
                if mx is not None:
                    restart = mx if dcat.engine_major < 4 else mx + (c["identity_increment"] or 1)
                    dcur.execute(f"ALTER TABLE {quote_ident(t)} ALTER {quote_ident(c['name'])} RESTART WITH {restart}")
                    dst.commit()
        log(f"{t}: copied {n} row(s)")
    src.commit()

    if create_tables and created:
        dcat2 = Catalog(dst)
        have = {t["name"] for t in dcat2.list_objects("tables")}
        for t in created:
            for c in scat.constraints(t):
                if c["type"] == "FOREIGN KEY" and c["ref_table"] in have:
                    try:
                        dcur.execute(f"ALTER TABLE {quote_ident(t)} ADD {scat._constraint_ddl(c)}")
                        dst.commit()
                    except Exception as exc:  # noqa: BLE001
                        dst.rollback()
                        log(f"{t}: foreign key not created: {str(exc).strip().splitlines()[-1]}")
            for i in scat.indices(relation=t):
                if not i["constraint_name"]:
                    try:
                        for stmt_sql in [s for s in scat._index_ddl(i).split(";") if s.strip()]:
                            dcur.execute(stmt_sql)
                        dst.commit()
                    except Exception as exc:  # noqa: BLE001
                        dst.rollback()
                        log(f"{t}: index {i['name']} not created: {str(exc).strip().splitlines()[-1]}")
        log("Created foreign keys and indices of the new tables (triggers and checks are not copied)")
    log(f"Done: {result['rows']} row(s) in {len(order)} table(s)")
    return result

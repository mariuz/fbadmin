#!/usr/bin/env python3
"""Compare fbadmin's two Firebird drivers: pure-Python firebirdsql vs native firebird-driver.

For every server port given, and for both drivers, through fbadmin's own connection layer
(``fbadmin.connections.connect``, the code path the app uses), it measures:

* connect + attach (+ detach) time
* a trivial query round trip (``SELECT 1 FROM RDB$DATABASE``), many iterations
* fetching 10k and 100k rows of a mixed-type table (rows/s, wall and client CPU time)
* inserting rows with parameters (one prepared statement, executed in a loop)
* BLOB write and read (1 MB, 10 MB)
* metadata load: the catalog queries behind the object tree and properties (lists of every
  object type, and DDL of every table) for a generated schema of a few hundred objects
* backup streaming through the Services API into a local file
* correctness: the same values of every tricky type serialized as the browser gets them

Each measurement runs ``--warmup`` times unmeasured and ``--repeat`` times measured; the report
gives the median and the spread (min-max). Output: JSON (``--json``) and a Markdown table
(``--md``, also printed). Usage (from the repository root, with the shared virtualenv)::

    .venv/bin/python scripts/driver-compare.py --ports 3053,3054,3050,3056 \\
        --fbclient build/firebird-embedded/lib/libfbclient.so.2 --json out.json --md out.md

Needs Firebird servers (SYSDBA/masterkey by default, see --help) and the native driver
(``firebird-driver``, a dependency of fbadmin, plus a libfbclient; ``scripts/embedded-test-setup.sh``
provides a Firebird 5 one). See docs/DRIVERS.md for results and caveats.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

DRIVERS = ("firebirdsql", "native")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default=os.environ.get("FBADMIN_TEST_HOST", "127.0.0.1"))
    p.add_argument("--ports", default=os.environ.get("FBADMIN_TEST_PORT", "3050"),
                   help="comma-separated server ports (default 3050)")
    p.add_argument("--user", default="SYSDBA")
    p.add_argument("--password", default=os.environ.get("FBADMIN_TEST_PASSWORD", "masterkey"))
    p.add_argument("--dir", default=os.environ.get("FBADMIN_TEST_DIR", "/var/lib/firebird/data"),
                   help="database directory on the server")
    p.add_argument("--fbclient", default=os.environ.get("FBADMIN_FBCLIENT"), help="libfbclient for the native driver")
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--roundtrips", type=int, default=1000, help="iterations of the trivial query")
    p.add_argument("--inserts", type=int, default=5000)
    p.add_argument("--rows", default="10000,100000", help="row counts for the fetch test")
    p.add_argument("--quick", action="store_true", help="small sizes, 2 repeats (smoke test of the script)")
    p.add_argument("--json", help="write the results as JSON here")
    p.add_argument("--md", help="write the Markdown report here")
    a = p.parse_args()
    if a.quick:
        a.repeat, a.roundtrips, a.inserts, a.rows = 2, 100, 500, "2000"
    return a


# ----------------------------------------------------------------------------- helpers
def stats(samples: list[float]) -> dict:
    return {"median": statistics.median(samples), "min": min(samples), "max": max(samples), "n": len(samples)}


def measure(fn, repeat: int, warmup: int) -> tuple[dict, dict]:
    """Run ``fn`` warmup+repeat times; (wall seconds stats, client CPU seconds stats).
    A test whose warm-up run takes longer than 5 s is measured 3 times at most."""
    t0 = time.perf_counter()
    for _ in range(warmup):
        fn()
    if warmup and (time.perf_counter() - t0) / warmup > 5:
        repeat = min(repeat, 3)
    wall, cpu = [], []
    for _ in range(repeat):
        c0, t0 = time.process_time(), time.perf_counter()
        fn()
        wall.append(time.perf_counter() - t0)
        cpu.append(time.process_time() - c0)
    return stats(wall), stats(cpu)


class Bench:
    def __init__(self, a, port: int):
        from fbadmin.store import ServerEntry

        self.a = a
        self.port = port
        self.srv = ServerEntry(name=f"bench-{port}", host=a.host, port=port, user=a.user)
        self.path = f"{a.dir}/fbadmin_bench_{port}_{uuid.uuid4().hex[:6]}.fdb"
        self.meta_path = f"{a.dir}/fbadmin_bench_meta_{port}_{uuid.uuid4().hex[:6]}.fdb"

    def connect(self, driver: str, path: str | None = None):
        from fbadmin.connections import connect

        return connect(self.srv, path or self.path, self.a.password, driver=driver)

    @contextmanager
    def conn(self, driver: str, path: str | None = None):
        c = self.connect(driver, path)
        try:
            yield c
        finally:
            c.close()

    def script(self, driver: str, sql: str, path: str | None = None) -> dict:
        from fbadmin.query import run_script

        with self.conn(driver, path) as c:
            r = run_script(c, sql, autocommit=True, max_rows=10 ** 9)
        if r["error"]:
            raise RuntimeError(r["error"]["message"])
        return r


# ----------------------------------------------------------------------------- setup
def mixed_columns(fb4: bool) -> str:
    cols = ("ID INTEGER NOT NULL PRIMARY KEY, I INTEGER, BI BIGINT, S VARCHAR(50) CHARACTER SET UTF8, "
            "N NUMERIC(18,4), D DOUBLE PRECISION, TS TIMESTAMP, DT DATE, B BOOLEAN")
    if fb4:
        cols += ", I128 INT128, DF DECFLOAT(34), TSZ TIMESTAMP WITH TIME ZONE"
    return cols


def setup(b: Bench, fb4: bool, max_rows: int) -> None:
    from fbadmin.connections import connect

    connect(b.srv, b.path, b.a.password, create=True, driver="firebirdsql").close()
    extra = (", :r * 1000000000000000000000, CAST(:r AS DECFLOAT(34)) / 7, "
             "CAST('2024-01-01 00:00:00 Europe/Bucharest' AS TIMESTAMP WITH TIME ZONE) + :r / 86400.0") if fb4 else ""
    b.script("firebirdsql", f"""
CREATE TABLE MIXED ({mixed_columns(fb4)});
CREATE TABLE INS ({mixed_columns(False)});
CREATE TABLE BLOBS (ID INTEGER NOT NULL PRIMARY KEY, B BLOB SUB_TYPE BINARY);
COMMIT;
EXECUTE BLOCK AS
  DECLARE r INTEGER = 1;
BEGIN
  WHILE (r <= {max_rows}) DO
  BEGIN
    INSERT INTO MIXED VALUES (:r, :r * 7, :r * 100000000000, 'row ' || :r || ' ✓ ünïcödé', :r / 3.0, :r / 7.0,
      DATEADD(:r SECOND TO TIMESTAMP '2024-01-01 00:00:00'), DATEADD(:r / 100 DAY TO DATE '2000-01-01'),
      MOD(:r, 2) = 0 {extra});
    r = r + 1;
  END
END;
COMMIT;
""")


def setup_metadata(b: Bench, tables: int = 100) -> int:
    """A schema of a few hundred objects: tables (+PK, index, FK), views, procedures, triggers."""
    from fbadmin.connections import connect

    connect(b.srv, b.meta_path, b.a.password, create=True, driver="firebirdsql").close()
    stmts = ["CREATE DOMAIN D_NAME AS VARCHAR(100) CHARACTER SET UTF8", "CREATE SEQUENCE S_GEN"]
    for i in range(tables):
        fk = f", PARENT_ID INTEGER REFERENCES T{i - 1:03d}(ID)" if i else ""
        stmts.append(f"CREATE TABLE T{i:03d} (ID INTEGER NOT NULL PRIMARY KEY, NAME D_NAME, AMOUNT NUMERIC(15,2), "
                     f"CREATED TIMESTAMP DEFAULT CURRENT_TIMESTAMP, NOTE BLOB SUB_TYPE TEXT, FLAG BOOLEAN, "
                     f"CODE CHAR(8){fk})")
        stmts.append(f"CREATE INDEX IX_T{i:03d}_NAME ON T{i:03d} (NAME)")
    stmts.append("COMMIT")
    for i in range(0, tables, 2):
        stmts.append(f"CREATE VIEW V{i:03d} AS SELECT ID, NAME, AMOUNT FROM T{i:03d} WHERE FLAG")
    stmts.append("SET TERM ^ ;")
    for i in range(0, tables, 2):
        stmts.append(f"CREATE PROCEDURE P{i:03d} (X INTEGER) RETURNS (Y INTEGER, N VARCHAR(100)) AS "
                     f"BEGIN FOR SELECT ID, NAME FROM T{i:03d} WHERE ID > :X INTO :Y, :N DO SUSPEND; END^")
        stmts.append(f"CREATE TRIGGER TR{i:03d} FOR T{i:03d} BEFORE INSERT AS "
                     f"BEGIN IF (NEW.ID IS NULL) THEN NEW.ID = NEXT VALUE FOR S_GEN; END^")
    stmts.append("SET TERM ; ^")
    sql = ""
    for s in stmts:
        sql += s + ("\n" if s.startswith("SET TERM") or s.endswith("^") else ";\n")
    b.script("firebirdsql", sql + "COMMIT;", b.meta_path)
    return tables * 2 + (tables // 2) * 3 + 2  # tables, indices, views, procedures, triggers, domain, sequence


def drop(b: Bench) -> None:
    for p in (b.path, b.meta_path):
        try:
            b.connect("firebirdsql", p).drop_database()
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not drop {p}: {exc})", file=sys.stderr)


# ----------------------------------------------------------------------------- tests
def t_connect(b: Bench, drv: str):
    def run():
        b.connect(drv).close()
    return run


def t_roundtrip(b: Bench, drv: str, conn):
    n = b.a.roundtrips

    def run():
        cur = conn.cursor()
        for _ in range(n):
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchall()
        conn.commit()
    return run


def t_fetch(drv: str, conn, rows: int):
    def run():
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM MIXED WHERE ID <= {rows}")
        got = cur.fetchall()
        assert len(got) == rows, len(got)
        conn.commit()
    return run


def t_fetch_raw(b: Bench, rows: int):
    """firebird-driver without fbadmin's adapter (shows the adapter's own cost)."""
    from fbadmin.native import _load
    from firebird.driver import connect as fd_connect

    _load()
    con = fd_connect(f"{b.a.host}/{b.port}:{b.path}", user=b.a.user, password=b.a.password, charset="UTF8")

    def run():
        cur = con.cursor()
        cur.execute(f"SELECT * FROM MIXED WHERE ID <= {rows}")
        assert len(cur.fetchall()) == rows
        cur.close()
        con.commit()
    return run, con


def t_insert(drv: str, conn, n: int):
    import datetime as dt
    import decimal

    def run():
        cur = conn.cursor()
        cur.execute("DELETE FROM INS")
        conn.commit()
        stmt = cur.prep("INSERT INTO INS VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        ts = dt.datetime(2024, 1, 1, 12, 0, 0)
        for i in range(n):
            cur.execute(stmt, [i, i * 3, i * 10 ** 10, f"name {i} ✓", decimal.Decimal(i) / 4, i / 3, ts,
                               ts.date(), i % 2 == 0])
        conn.commit()
    return run


def t_blob(drv: str, conn, size: int, op: str):
    data = os.urandom(size)
    state = {"id": 0}

    def write():
        state["id"] += 1
        cur = conn.cursor()
        cur.execute("INSERT INTO BLOBS VALUES (?, ?)", [state["id"], data])
        conn.commit()

    def read():
        cur = conn.cursor()
        cur.execute("SELECT B FROM BLOBS WHERE ID = 1")
        assert len(cur.fetchone()[0]) == size
        conn.commit()

    if op == "read":
        cur = conn.cursor()
        cur.execute("DELETE FROM BLOBS")
        cur.execute("INSERT INTO BLOBS VALUES (1, ?)", [data])
        conn.commit()
        return read
    cur = conn.cursor()
    cur.execute("DELETE FROM BLOBS")
    conn.commit()
    state["id"] = 0
    return write


def t_metadata(b: Bench, drv: str):
    """The object tree's lists and every table's properties/DDL, on a fresh connection per run.

    A fresh connection, because firebirdsql keeps the statements of closed cursors allocated on
    the server until the connection ends: on Firebird 5 (shared statement cache) one connection
    that loads this schema a few times fails with "Too many concurrent executions of the same
    request". See docs/DRIVERS.md."""
    from fbadmin.metadata.catalog import Catalog

    types = ("tables", "views", "procedures", "functions", "packages", "triggers", "sequences", "domains",
             "exceptions", "indices", "roles")

    def run():
        with b.conn(drv, b.meta_path) as conn:
            cat = Catalog(conn)
            cat.object_counts()
            tables = []
            for t in types:
                items = cat.list_objects(t)
                if t == "tables":
                    tables = [o["name"] for o in items]
            for name in tables:
                cat.get_object("tables", name)
            conn.commit()
    return run


def t_backup(b: Bench, drv: str, major: int, dest: Path):
    from fbadmin.maintenance.ops import BackupOptions, backup_stream
    from fbadmin.native import connect_services
    from firebirdsql import services

    def run():
        kw = {"host": b.a.host, "port": b.port, "user": b.a.user, "password": b.a.password, "timeout": 60}
        svc = connect_services(**kw) if drv == "native" else services.connect(**kw)
        try:
            with open(dest, "wb") as f:
                f.writelines(backup_stream(svc, b.path, BackupOptions(), major))
        finally:
            svc.close()
    return run


CORRECTNESS = [
    ("SMALLINT / INTEGER / BIGINT", "SELECT CAST(-32768 AS SMALLINT), 2147483647, -9223372036854775808 FROM RDB$DATABASE", 3),
    ("NUMERIC(18,4) / DECIMAL", "SELECT CAST('-12345678901234.5678' AS NUMERIC(18,4)), CAST(0.01 AS DECIMAL(9,2)) FROM RDB$DATABASE", 3),
    ("FLOAT / DOUBLE", "SELECT CAST(1.5 AS FLOAT), CAST(1e-300 AS DOUBLE PRECISION), 1e0 / 3 FROM RDB$DATABASE", 3),
    ("DATE / TIME / TIMESTAMP", "SELECT DATE '0001-01-01', TIME '23:59:59.9999', TIMESTAMP '9999-12-31 23:59:59.9999' FROM RDB$DATABASE", 3),
    ("BOOLEAN / NULL", "SELECT TRUE, FALSE, CAST(NULL AS INTEGER) FROM RDB$DATABASE", 3),
    ("CHAR / VARCHAR UTF8", "SELECT CAST('abc' AS CHAR(10)), CAST('héllo ✓ 漢字 😀' AS VARCHAR(50) CHARACTER SET UTF8) FROM RDB$DATABASE", 3),
    ("VARCHAR WIN1252 / NONE / OCTETS", ("SELECT CAST('café €' AS VARCHAR(20) CHARACTER SET WIN1252), CAST('ascii' AS VARCHAR(10) CHARACTER SET NONE), "
     "CAST(x'00FF10' AS VARCHAR(3) CHARACTER SET OCTETS) FROM RDB$DATABASE"), 3),
    ("BLOB text / binary", "SELECT CAST('naïve ✓' AS BLOB SUB_TYPE TEXT), CAST(x'DEADBEEF' AS BLOB SUB_TYPE BINARY) FROM RDB$DATABASE", 3),
    ("INT128", "SELECT CAST('170141183460469231731687303715884105727' AS INT128), CAST(-5 AS INT128) FROM RDB$DATABASE", 4),
    ("NUMERIC(38,6)", "SELECT CAST('-12345678901234567890123456789012.123456' AS NUMERIC(38,6)) FROM RDB$DATABASE", 4),
    ("DECFLOAT(16) / DECFLOAT(34)", ("SELECT CAST('-9.999999999999999E+384' AS DECFLOAT(16)), CAST(1 AS DECFLOAT(34)) / 3, "
     "CAST('NaN' AS DECFLOAT(34)), CAST('-Infinity' AS DECFLOAT(16)) FROM RDB$DATABASE"), 4),
    ("TIME / TIMESTAMP WITH TIME ZONE", ("SELECT CAST('10:00 Europe/London' AS TIME WITH TIME ZONE), "
     "CAST('2024-02-29 12:00:00.1234 +05:30' AS TIMESTAMP WITH TIME ZONE), "
     "CAST('2024-10-27 01:30 Europe/London' AS TIMESTAMP WITH TIME ZONE) FROM RDB$DATABASE"), 4),
]


def correctness(b: Bench, major: int) -> list[dict]:
    out = []
    for label, sql, need in CORRECTNESS:
        if major < need:
            continue
        vals = {}
        for drv in DRIVERS:
            try:
                r = b.script(drv, sql)
                vals[drv] = r["results"][0]["rows"][0]
            except Exception as exc:  # noqa: BLE001
                vals[drv] = f"ERROR: {exc}"
        out.append({"type": label, "identical": vals["firebirdsql"] == vals["native"], **vals})
    return out


# ----------------------------------------------------------------------------- driver
def run_port(a, port: int) -> dict:
    b = Bench(a, port)
    rows_list = [int(x) for x in a.rows.split(",") if x]
    from fbadmin.connections import connect
    from fbadmin.native import client_major

    connect(b.srv, b.path, a.password, create=True, driver="firebirdsql").close()
    c = b.connect("firebirdsql")
    cur = c.cursor()
    cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE")
    version = cur.fetchone()[0]
    c.drop_database()
    major = int(version.split(".")[0])
    # Firebird 4+ types need a Firebird 4+ client library on the native side
    fb4 = major >= 4 and client_major() >= 4
    print(f"== port {port}: Firebird {version}", file=sys.stderr)
    setup(b, fb4, max(rows_list))
    n_objects = setup_metadata(b)
    res: dict = {"port": port, "engine_version": version, "fb4_types": fb4, "metadata_objects": n_objects, "tests": {}}
    tmp = Path(tempfile.mkdtemp(prefix="fbadmin-bench-"))
    try:
        def rec(name: str, drv: str, fn, unit_count: float | None = None, unit: str | None = None, repeat=None):
            wall, cpu = measure(fn, repeat or a.repeat, a.warmup)
            entry = res["tests"].setdefault(name, {"unit": unit, "count": unit_count})
            entry[drv] = {"wall": wall, "cpu": cpu}
            print(f"  {name:28s} {drv:12s} median {wall['median'] * 1000:9.1f} ms  "
                  f"(min {wall['min'] * 1000:.1f}, max {wall['max'] * 1000:.1f}; cpu {cpu['median'] * 1000:.1f} ms)",
                  file=sys.stderr)

        for drv in DRIVERS:
            with b.conn(drv) as conn:
                cur = conn.cursor()
                plugin = ", a.MON$WIRE_CRYPT_PLUGIN" if major >= 4 else ""
                cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'WIRE_ENCRYPTED'), "
                            f"RDB$GET_CONTEXT('SYSTEM', 'WIRE_COMPRESSED'){plugin} FROM MON$ATTACHMENTS a "
                            "WHERE a.MON$ATTACHMENT_ID = CURRENT_CONNECTION")
                w = cur.fetchone()
                res.setdefault("wire", {})[drv] = (
                    f"encrypted={w[0]}" + (f" ({(w[2] or '').strip()})" if major >= 4 else "") + f", compressed={w[1]}")
                conn.commit()
            rec("connect + attach + detach", drv, t_connect(b, drv), 1, "connection", repeat=max(a.repeat, 10))
            with b.conn(drv) as conn:
                rec(f"round trip ×{a.roundtrips}", drv, t_roundtrip(b, drv, conn), a.roundtrips, "query")
                for n in rows_list:
                    rec(f"fetch {n:,} rows", drv, t_fetch(drv, conn, n), n, "row")
                rec(f"insert {a.inserts:,} rows (params)", drv, t_insert(drv, conn, a.inserts), a.inserts, "row")
                for size in (1, 10):
                    rec(f"BLOB write {size} MB", drv, t_blob(drv, conn, size * 1024 * 1024, "write"), size, "MB")
                    rec(f"BLOB read {size} MB", drv, t_blob(drv, conn, size * 1024 * 1024, "read"), size, "MB")
                cur = conn.cursor()
                cur.execute("DELETE FROM BLOBS")  # the backup below covers MIXED and INS only
                conn.commit()
            rec(f"metadata load ({n_objects} objects)", drv, t_metadata(b, drv), n_objects, "object")
            dest = tmp / f"backup-{drv}.fbk"
            rec("backup stream to local file", drv, t_backup(b, drv, major, dest))
            res.setdefault("backup_bytes", dest.stat().st_size)
        n = max(rows_list)
        run_raw, con = t_fetch_raw(b, n)
        try:
            rec(f"fetch {n:,} rows", "native (raw firebird-driver)", run_raw, n, "row")
        finally:
            con.close()
        res["correctness"] = correctness(b, major if fb4 else min(major, 3))
    finally:
        drop(b)
        for f in tmp.iterdir():
            f.unlink()
        tmp.rmdir()
    return res


def environment(a) -> dict:
    import firebirdsql
    from fbadmin import drivers

    os.environ.setdefault("FBADMIN_FBCLIENT", a.fbclient or "")
    nat = drivers.native_status()
    try:
        from importlib.metadata import version

        fsql = version("firebirdsql")
    except Exception:  # noqa: BLE001
        fsql = getattr(firebirdsql, "__version__", "?")
    return {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(),
            "cpus": os.cpu_count(), "firebirdsql": fsql, "firebird_driver": nat["package"],
            "libfbclient": nat["library"], "client_version": nat["client_version"],
            "repeat": a.repeat, "warmup": a.warmup, "date": time.strftime("%Y-%m-%d")}


def fmt_ms(s: float) -> str:
    ms = s * 1000
    return f"{ms:,.0f}" if ms >= 100 else f"{ms:,.1f}" if ms >= 1 else f"{ms:.3f}"


def markdown(env: dict, results: list[dict]) -> str:
    out = [(f"Environment: Python {env['python']}, {env['platform']}, {env['cpus']} CPUs; "
           f"firebirdsql {env['firebirdsql']}; firebird-driver {env['firebird_driver']} with "
           f"libfbclient {env['client_version']}. Median of {env['repeat']} runs after {env['warmup']} warm-up "
           "(min–max in brackets); times in ms, lower is better. *ratio* = firebirdsql time / native time."), ""]
    for r in results:
        out.append(f"#### Firebird {r['engine_version']} (port {r['port']})")
        out.append("")
        out.append("| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for name, t in r["tests"].items():
            p, n = t.get("firebirdsql"), t.get("native")
            if not p or not n:
                continue
            ratio = p["wall"]["median"] / n["wall"]["median"] if n["wall"]["median"] else float("nan")

            def cell(x):
                w = x["wall"]
                return f"{fmt_ms(w['median'])} ({fmt_ms(w['min'])}–{fmt_ms(w['max'])})"
            out.append(f"| {name} | {cell(p)} | {cell(n)} | {ratio:.2f}× | {fmt_ms(p['cpu']['median'])} | "
                       f"{fmt_ms(n['cpu']['median'])} |")
            raw = t.get("native (raw firebird-driver)")
            if raw:
                out.append(f"| {name}, firebird-driver without fbadmin's adapter | | {cell(raw)} | | | "
                           f"{fmt_ms(raw['cpu']['median'])} |")
        out.append("")
        wire = "; ".join(f"{d}: {w}" for d, w in r.get("wire", {}).items())
        out.append(f"Backup size {r.get('backup_bytes', 0) / 1e6:.1f} MB. Wire: {wire}.")
        out.append("")
        diffs = [c for c in r.get("correctness", []) if not c["identical"]]
        out.append(f"Correctness: {len(r.get('correctness', [])) - len(diffs)} of {len(r.get('correctness', []))} "
                   "type groups serialize identically" + (":" if diffs else "."))
        for c in diffs:
            out.append(f"- {c['type']}: firebirdsql `{c['firebirdsql']}` vs native `{c['native']}`")
        out.append("")
    return "\n".join(out)


def main() -> int:
    a = parse_args()
    if a.fbclient:
        os.environ["FBADMIN_FBCLIENT"] = a.fbclient
    os.environ.pop("FBADMIN_DRIVER", None)
    data_dir = tempfile.mkdtemp(prefix="fbadmin-bench-data-")
    os.environ["FBADMIN_DATA_DIR"] = data_dir  # never read a developer's stored driver setting
    from fbadmin import config

    config.settings.data_dir = Path(data_dir)
    from fbadmin.native import native_available

    ok, why = native_available()
    if not ok:
        print(f"native driver unavailable: {why} (use --fbclient)", file=sys.stderr)
        return 2
    from fbadmin.security import wirecompat

    wirecompat.apply()  # the app's firebirdsql fixes, as in the server

    env = environment(a)
    results = []
    for p in a.ports.split(","):
        if not p.strip():
            continue
        results.append(run_port(a, int(p)))
        if a.json:  # partial results survive a failure on a later port
            Path(a.json).write_text(json.dumps({"environment": env, "results": results}, indent=2,
                                               ensure_ascii=False), "utf-8")
    doc = {"environment": env, "results": results}
    md = markdown(env, results)
    if a.json:
        Path(a.json).write_text(json.dumps(doc, indent=2, ensure_ascii=False), "utf-8")
    if a.md:
        Path(a.md).write_text(md + "\n", "utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

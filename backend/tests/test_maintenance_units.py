"""Unit tests for maintenance helpers that need no server: gstat parser, cron, trace parsing, SPB parsing."""

from __future__ import annotations

import struct
from datetime import datetime
from pathlib import Path

import pytest

from fbadmin.maintenance import gstat, svc, trace
from fbadmin.maintenance.cron import Cron, CronError

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- gstat
@pytest.mark.parametrize("version", [3, 4, 5])
def test_gstat_parser_real_outputs(version):
    stats = gstat.parse((FIX / f"gstat_fb{version}.txt").read_text())
    hdr = stats["header"]
    assert hdr["page_size"] == 4096
    assert hdr["database_dialect"] == 3
    assert hdr["attributes"] == ["force write"]
    assert hdr["ods_version"] == {3: 12.0, 4: 13.0, 5: 13.1}[version]
    assert stats["database"].endswith(".fdb")

    tables = {t["name"]: t for t in stats["tables"]}
    assert set(tables) == {"CUSTOMER", "WIDE_KEYS", "EMPTY_T"}
    cust = tables["CUSTOMER"]
    assert cust["total_records"] == 3000
    assert cust["total_versions"] == 1500
    assert cust["max_versions"] == 1
    assert cust["version_ratio"] == 0.5
    assert cust["blobs"] == 3000
    assert cust["blob_levels"]["0"] == 3000
    assert sum(cust["fill_distribution"]) == cust["data_pages"]
    assert cust["table_size"] > 0
    assert 0 < cust["average_fill"] <= 100
    idx = {i["name"]: i for i in cust["indices"]}
    assert set(idx) == {"IX_CUST_NAME", "RDB$PRIMARY1"}
    assert idx["RDB$PRIMARY1"]["nodes"] == 3000 and idx["RDB$PRIMARY1"]["depth"] == 2
    assert idx["IX_CUST_NAME"]["max_dup"] == 59
    assert idx["IX_CUST_NAME"]["average_key_length"] == 2.05
    assert sum(idx["RDB$PRIMARY1"]["fill_distribution"]) == idx["RDB$PRIMARY1"]["leaf_buckets"]

    wide = tables["WIDE_KEYS"]
    assert wide["indices"][0]["name"] == "IX_WIDE" and wide["indices"][0]["depth"] == 3
    assert wide["indices"][0]["leaf_buckets"] == 100
    assert tables["EMPTY_T"]["total_records"] == 0 and tables["EMPTY_T"]["indices"] == []

    findings = gstat.attention(stats)
    issues = {(f["table"], f["index"], f["issue"]) for f in findings}
    assert ("CUSTOMER", None, "Many record versions") in issues
    assert ("WIDE_KEYS", None, "Many record versions") in issues
    assert not any(f["issue"] == "Deep index" for f in findings)
    deeper = gstat.attention(stats, {"index_depth": 3})
    assert ("WIDE_KEYS", "IX_WIDE", "Deep index") in {(f["table"], f["index"], f["issue"]) for f in deeper}


def test_gstat_header_and_shutdown_attributes():
    text = ("Database header page information:\n\tFlags\t\t\t0\n\tPage size\t\t8192\n\tPage buffers\t\t3000\n"
            "\tDatabase dialect\t1\n\tAttributes\t\tforce write, no reserve, read only, full shutdown\n\n"
            "    Variable header data:\n\tSweep interval:\t\t12345\n\t*END*\n")
    hdr = gstat.parse_header(text.splitlines())
    assert hdr["page_size"] == 8192 and hdr["page_buffers"] == 3000 and hdr["sweep_interval"] == 12345
    assert hdr["attributes"] == ["force write", "no reserve", "read only", "full shutdown"]
    assert gstat.shutdown_mode(hdr["attributes"]) == "full"
    assert gstat.shutdown_mode(["single-user maintenance"]) == "single"
    assert gstat.shutdown_mode(["multi-user maintenance"]) == "multi"
    assert gstat.shutdown_mode([]) == "online"


def test_gstat_transaction_gap_and_fragments():
    stats = {"header": {"next_transaction": 50000, "oldest_active": 10},
             "tables": [{"name": "T", "total_records": 1000, "total_fragments": 500, "fragment_ratio": 0.5,
                         "version_ratio": 2.0, "total_versions": 2000, "max_versions": 9, "data_pages": 100,
                         "average_fill": 30, "indices": [{"name": "I", "depth": 5, "nodes": 2000, "max_dup": 1990}]}]}
    issues = [(f["severity"], f["issue"]) for f in gstat.attention(stats)]
    assert issues[0] == ("critical", "Many record versions")
    for name in ("Transaction gap", "Long version chains", "Fragmented records", "Low page fill", "Deep index",
                 "Poor selectivity"):
        assert name in [i for _, i in issues]


# ----------------------------------------------------------------- cron
def test_cron_basic_fields():
    c = Cron("*/15 2 * * *")
    assert c.minutes == {0, 15, 30, 45} and c.hours == {2}
    assert c.next(datetime(2026, 1, 1, 1, 59)) == datetime(2026, 1, 1, 2, 0)
    assert c.next(datetime(2026, 1, 1, 2, 0)) == datetime(2026, 1, 1, 2, 15)
    assert c.next(datetime(2026, 1, 1, 2, 50)) == datetime(2026, 1, 2, 2, 0)


def test_cron_names_ranges_lists_steps():
    c = Cron("30 1 * jan,jul mon-fri")
    assert c.months == {1, 7} and c.dows == {1, 2, 3, 4, 5}
    # 2026-01-03 is a Saturday -> next weekday is Monday 5th
    assert c.next(datetime(2026, 1, 3, 0, 0)) == datetime(2026, 1, 5, 1, 30)
    assert c.next(datetime(2026, 1, 31, 2, 0)) == datetime(2026, 7, 1, 1, 30)
    assert Cron("0 0 * * 7").dows == {0}
    assert Cron("10-50/20 * * * *").minutes == {10, 30, 50}
    assert Cron("5/20 * * * *").minutes == {5, 25, 45}


def test_cron_dom_dow_or_and_macros():
    c = Cron("0 12 1 * sun")  # 1st of month OR any Sunday
    assert c.next(datetime(2026, 3, 1, 13, 0)) == datetime(2026, 3, 8, 12, 0)  # next Sunday
    assert c.next(datetime(2026, 3, 29, 13, 0)) == datetime(2026, 4, 1, 12, 0)  # 1st
    assert Cron("@daily").next(datetime(2026, 5, 5, 10, 0)) == datetime(2026, 5, 6, 0, 0)
    assert Cron("@hourly").next(datetime(2026, 5, 5, 10, 0)) == datetime(2026, 5, 5, 11, 0)
    assert Cron("@monthly").next(datetime(2026, 12, 15)) == datetime(2027, 1, 1)
    assert Cron("0 0 29 2 *").next(datetime(2026, 1, 1)) == datetime(2028, 2, 29)


@pytest.mark.parametrize("expr", ["", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                                  "*/0 * * * *", "a * * * *", "5-1 * * * *", "0 0 31 2 *", "1,,2 * * * *"])
def test_cron_invalid(expr):
    with pytest.raises(CronError):
        Cron(expr).next(datetime(2026, 1, 1))


# ---------------------------------------------------------------- trace
SAMPLE_TRACE = """Trace session ID 3 started
2026-10-18T19:52:00.6270 (23:0x7fb1d1355e40) ATTACH_DATABASE
\t/data/x.fdb (ATT_4, SYSDBA:NONE, UTF8, TCPv4:172.17.0.1/51100)
\tclient.py:4792

2026-10-18T19:52:00.6320 (23:0x7fb1d1355e40) EXECUTE_STATEMENT_FINISH
\t/data/x.fdb (ATT_4, SYSDBA:NONE, UTF8, TCPv4:172.17.0.1/51100)
\t\t(TRA_4, READ_COMMITTED | READ_CONSISTENCY | WAIT | READ_WRITE)

Statement 103:
-------------------------------------------------------------------------------
select 42 as magic
from rdb$database
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
PLAN (RDB$DATABASE NATURAL)
1 records fetched
     17 ms, 5 fetch(es)

2026-10-18T19:52:00.6340 (23:0x7fb1d1355e40) DETACH_DATABASE
\t/data/x.fdb (ATT_4, SYSDBA:NONE, UTF8, TCPv4:172.17.0.1/51100)
"""


class _Sink:
    def write(self, s):
        pass


def test_trace_event_parsing():
    sess = trace.TraceSession("srv", "t", "", "all_statements", None)
    for line in SAMPLE_TRACE.splitlines():
        sess.feed(line, _Sink())
    sess._flush()
    assert sess.session_id == 3 and sess.status == "running"
    evs = sess.events_after(0)
    assert [e["type"] for e in evs] == ["ATTACH_DATABASE", "EXECUTE_STATEMENT_FINISH", "DETACH_DATABASE"]
    ex = evs[1]
    assert ex["sql"] == "select 42 as magic\nfrom rdb$database"
    assert ex["plan"] == "PLAN (RDB$DATABASE NATURAL)"
    assert ex["duration_ms"] == 17 and ex["attachment"] == 4 and ex["user"] == "SYSDBA"
    assert [e["seq"] for e in sess.events_after(1)] == [2, 3]
    assert len(sess.events_after(0, q="MAGIC")) == 1
    assert len(sess.events_after(0, types=["ATTACH", "DETACH"])) == 2


def test_trace_config_templates():
    cfg = trace.build_config("slow_queries", "/var/lib/firebird/data/my-db_1.fdb", time_threshold=250)
    assert cfg.startswith("database = %[\\\\/]my_db_1.fdb\n{")
    assert "time_threshold = 250" in cfg and "log_statement_finish = true" in cfg
    assert trace.build_config("errors").startswith("database\n{")
    assert 'include_filter = "%SELECT%"' in trace.build_config("all_statements", include_filter="%SELECT%")
    assert trace.build_config("custom", custom="database { enabled = true }") == "database { enabled = true }"
    with pytest.raises(ValueError):
        trace.build_config("nope")
    with pytest.raises(ValueError):
        trace.build_config("custom", custom=" ")


def test_trace_session_list_parsing():
    lines = [" ", "Session ID: 1 ", "  name:  fbadmin-exp ", "  user:  SYSDBA ", "  date:  2026-10-18 19:51:59 ",
             "  flags: active, trace ", "Session ID: 7", "  user:  BOB", "  flags: suspend, log full"]
    s = trace.parse_session_list(lines)
    assert s[0] == {"id": 1, "name": "fbadmin-exp", "user": "SYSDBA", "date": "2026-10-18 19:51:59",
                    "flags": ["active", "trace"]}
    assert s[1]["id"] == 7 and s[1]["flags"] == ["suspend", "log full"]


# ------------------------------------------------------------------ SPB
def test_spb_builder_and_limbo_parser():
    spb = svc.Spb(svc.ACT_BACKUP).str(svc.SPB_DBNAME, "db").int(svc.SPB_OPTIONS, 5).byte(12, 39).flag(107)
    assert spb.raw() == bytes([1, 106, 2, 0]) + b"db" + bytes([108, 5, 0, 0, 0, 12, 39, 107])
    buf = (bytes([svc.INFO_SVC_LIMBO_TRANS, 0, 0, svc.SINGLE_TRA_ID]) + struct.pack("<I", 2)
           + bytes([svc.MULTI_TRA_ID]) + struct.pack("<I", 9) + bytes([svc.TRA_HOST_SITE, 3, 0]) + b"h01"
           + bytes([svc.TRA_STATE, 22, svc.TRA_ADVISE, 30, svc.TRA_DB_PATH, 4, 0]) + b"/x.f"
           + bytes([svc.SINGLE_TRA_ID_64]) + struct.pack("<Q", 2**33) + bytes([svc.INFO_END]))
    items = svc.parse_limbo(buf)
    assert [i["id"] for i in items] == [2, 9, 2**33]
    assert items[1]["multi"] and items[1]["host_site"] == "h01" and items[1]["state"] == "limbo"
    assert items[1]["advise"] == "commit" and items[1]["db_path"] == "/x.f"

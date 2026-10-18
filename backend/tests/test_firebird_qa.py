"""Compatibility tests against the firebird-qa corpus of DDL (optional, slow).

Enable with a checkout:  FBADMIN_QA_DIR=/path/to/firebird-qa pytest tests/test_firebird_qa.py
or let the test clone it: FBADMIN_QA_CLONE=1 (shallow clone into ~/.cache/fbadmin/firebird-qa).

* ``test_splitter_matches_isql`` - every script in the corpus is split by split_sql() and by
  an isql-rules reference splitter; the results must match.
* ``test_ddl_round_trip_sample`` - a deterministic sample of scripts from the DDL-heavy areas
  is run into a fresh database (statement by statement, errors ignored: many QA scripts fail
  on purpose), every user object's DDL is extracted, replayed into a second database and
  extracted again; text must be identical and every statement must replay.
  ``FBADMIN_QA_SAMPLE`` (default 25) sets the sample size, ``FBADMIN_QA_SEED`` the seed.
"""

from __future__ import annotations

import os
import random
import re
import uuid

import pytest

from fbadmin.sqlsplit import split_sql

from .conftest import DB_DIR, requires_server
from .qa_corpus import corpus, qa_checkout, reference_split, sql_only

pytestmark = pytest.mark.slow

QA = qa_checkout()
requires_qa = pytest.mark.skipif(QA is None, reason="set FBADMIN_QA_DIR or FBADMIN_QA_CLONE=1 (firebird-qa)")

DDL_AREAS = ("table", "domain", "view", "procedure", "trigger", "index", "fkey", "package", "generator",
             "exception", "role", "datatypes", "intl")
# Extraction order, same as the built-in round-trip test; replay retries resolve the rest.
ORDER = ["domains", "sequences", "exceptions", "roles", "tables", "functions", "procedures", "packages",
         "views", "triggers"]


@requires_qa
def test_splitter_matches_isql():
    items = corpus(QA)
    assert len(items) > 500, "firebird-qa checkout looks incomplete"
    # Scripts using isql's SET AUTOTERM (Firebird 6) split PSQL without SET TERM, which the
    # reference (classic isql rules) cannot do; split_sql handles them, but has no oracle there.
    items = [(p, s) for p, s in items if "autoterm" not in s.lower()]
    mismatches = []
    for path, script in items:
        ours = [s.sql for s in split_sql(script)]
        ref = reference_split(script)
        if sql_only(ours) != sql_only(ref):
            mismatches.append(path)
    print(f"\nsplitter: {len(items)} scripts, {len(mismatches)} differ from isql rules")
    for p in mismatches[:20]:
        print("  ", p)
    assert not mismatches, f"{len(mismatches)} scripts split differently, e.g. {mismatches[:5]}"


_GEN_VALUE = re.compile(r"^SET GENERATOR \S+ TO -?\d+;\n?", re.MULTILINE)


def _schema_only(ddl: str | None) -> str | None:
    """Sequence values are state, not schema (database triggers in the corpus bump them on replay)."""
    return None if ddl is None else _GEN_VALUE.sub("", ddl)


def _base(sid, dbid):
    return f"/api/servers/{sid}/databases/{dbid}"


def _new_db(client, sid, tag):
    r = client.post(f"/api/servers/{sid}/databases/create",
                    json={"name": tag, "path": f"{DB_DIR}/fbadmin_qa_{tag}_{uuid.uuid4().hex[:8]}.fdb"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run_each(client, sid, dbid, stmts: list[str]) -> list[tuple[str, str]]:
    """Execute statements one by one (autocommit); returns [(stmt, error)] for failures."""
    errors = []
    for s in stmts:
        res = client.post(_base(sid, dbid) + "/execute", json={"sql": s}).json()
        if res.get("error"):
            errors.append((s, res["error"]["message"]))
    return errors


def _extract_all(client, sid, dbid) -> dict[tuple[str, str], str]:
    out = {}
    for otype in ORDER:
        r = client.get(_base(sid, dbid) + f"/objects/{otype}")
        if r.status_code != 200:
            continue
        for item in r.json():
            if otype == "triggers" and item.get("relation"):
                continue  # table/view triggers are part of their relation's DDL
            d = client.get(_base(sid, dbid) + f"/objects/{otype}/{item['name']}")
            if d.status_code == 200 and d.json().get("ddl"):
                out[(otype, item["name"])] = d.json()["ddl"]
    return out


def _replay(client, sid, dbid, ddl: dict) -> list[tuple[str, str]]:
    """Run every statement of the extracted DDL, retrying failures until no progress."""
    pending = [s.sql for text in ddl.values() for s in split_sql(text)]
    for _ in range(6):
        failed = _run_each(client, sid, dbid, pending)
        if len(failed) in (0, len(pending)):
            return failed
        pending = [s for s, _e in failed]
    return failed


@requires_qa
@requires_server
def test_ddl_round_trip_sample(client, server):
    sid = server["id"]
    items = [(p, s) for p, s in corpus(QA, DDL_AREAS) if "create" in s.lower()]
    rnd = random.Random(int(os.environ.get("FBADMIN_QA_SEED", "1")))
    sample = rnd.sample(items, min(int(os.environ.get("FBADMIN_QA_SAMPLE", "25")), len(items)))
    problems = []
    for path, script in sample:
        src, dst = _new_db(client, sid, "src"), _new_db(client, sid, "dst")
        try:
            _run_each(client, sid, src, sql_only([s.sql for s in split_sql(script)]))
            ddl = _extract_all(client, sid, src)
            failed = _replay(client, sid, dst, ddl)
            for stmt, err in failed:
                problems.append(f"{path}: replay failed: {err.splitlines()[-1] if err else ''}\n    {stmt[:300]}")
            if not failed:
                again = _extract_all(client, sid, dst)
                for key, text in ddl.items():
                    if _schema_only(again.get(key)) != _schema_only(text):
                        problems.append(f"{path}: {key[0]} {key[1]} differs after round trip:\n{text}\n---\n"
                                        f"{again.get(key)}")
        finally:
            for d in (src, dst):
                client.post(f"/api/servers/{sid}/databases/{d}/drop")
    print(f"\nfirebird-qa DDL round trip: {len(sample)} scripts, {len(problems)} problems")
    for p in problems:
        print(p)
    assert not problems, f"{len(problems)} problems; first: {problems[0]}"

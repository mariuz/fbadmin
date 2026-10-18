"""Schema diff and migration script.

``compare(source, target)`` lists the objects whose DDL differs and builds a script that makes the
*target* database match the *source* (pgAdmin semantics: source = desired schema, target = database
to update).

The script runs in a safe order:

1. revoke privileges and remove comments that only the target has
2. detach PSQL that uses anything about to change: affected triggers and package bodies are dropped,
   affected procedures and functions are altered to stubs with the same signature (the isql
   technique), so tables, columns, views and routines can be dropped or altered
3. drop views that must be recreated (removed, column list changed, or depending on changed objects -
   together with every view that depends on them), foreign keys, indices, constraints, columns, tables
4. create or alter domains, sequences, exceptions, tables, columns, keys, new routines (stubs),
   package headers, views (dependency order), then every routine body via CREATE OR ALTER, package
   bodies, deferred computed columns, check constraints, indices, triggers
5. drop removed routines, packages, exceptions, sequences, domains, collations and roles
6. grant privileges and set comments (also re-applied to objects that were recreated)
"""

from __future__ import annotations

import re
from typing import Any

from ..metadata.types import quote_ident
from .extract import Section, render
from .model import Snapshot, column_fragment

TYPE_ORDER = ["database", "collations", "roles", "domains", "sequences", "exceptions", "tables", "indices",
              "views", "functions", "procedures", "packages", "triggers", "grants"]

_PSQL_DEP_TYPES = (2, 5, 15, 19)  # trigger, procedure, function, package body


def compare_objects(src: Snapshot, tgt: Snapshot) -> list[dict[str, Any]]:
    so, to = src.objects(), tgt.objects()
    items = []
    for key in sorted(set(so) | set(to), key=lambda k: (TYPE_ORDER.index(k[0]) if k[0] in TYPE_ORDER else 99, k[1])):
        a, b = so.get(key), to.get(key)
        if a == b:
            continue
        items.append({
            "type": key[0], "name": key[1],
            "status": "create" if b is None else "drop" if a is None else "alter",
            "source_ddl": a, "target_ddl": b,
        })
    return items


def _comment_object(key: str) -> str:
    """Name of the object a COMMENT ON target refers to (for COLUMN/PARAMETER the parent)."""
    rest = key[len("COMMENT ON "):]
    for kw in ("PROCEDURE PARAMETER ", "FUNCTION PARAMETER ", "COLUMN "):
        if rest.startswith(kw):
            return _unquote(_split_dotted(rest[len(kw):])[0])
    for kw in ("EXTERNAL FUNCTION ", "COLLATION ", "DOMAIN ", "SEQUENCE ", "EXCEPTION ", "ROLE ", "PACKAGE ",
               "TRIGGER ", "INDEX ", "TABLE ", "VIEW ", "PROCEDURE ", "FUNCTION "):
        if rest.startswith(kw):
            return _unquote(rest[len(kw):])
    return ""


def _split_dotted(s: str) -> list[str]:
    parts, cur, inq = [], "", False
    for ch in s:
        if ch == '"':
            inq = not inq
        if ch == "." and not inq:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _unquote(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('""', '"')
    return s


_INTS = ["SMALLINT", "INTEGER", "BIGINT", "INT128"]
_TEXT = re.compile(r"(VARCHAR|CHAR)\((\d+)\)(.*)")
_NUM = re.compile(r"(NUMERIC|DECIMAL)\((\d+),(\d+)\)")


def _widening(old: str, new: str) -> bool:
    """True when ALTER COLUMN TYPE can change ``old`` to ``new`` in place without losing data."""
    if old == new:
        return True
    m1, m2 = _TEXT.fullmatch(old), _TEXT.fullmatch(new)
    if m1 and m2:
        return m1.group(1) == m2.group(1) and m1.group(3) == m2.group(3) and int(m2.group(2)) >= int(m1.group(2))
    if old in _INTS and new in _INTS:
        return _INTS.index(new) >= _INTS.index(old)
    n1, n2 = _NUM.fullmatch(old), _NUM.fullmatch(new)
    if n1 and n2:
        p1, s1, p2, s2 = int(n1.group(2)), int(n1.group(3)), int(n2.group(2)), int(n2.group(3))
        return s2 >= s1 and p2 - s2 >= p1 - s1
    if old in _INTS and (m2 or n2):
        return True  # integers convert to wider text and decimals
    # domains and other types: let the server decide
    return not (m1 or n1 or old in _INTS)


class Migration:
    def __init__(self, src: Snapshot, tgt: Snapshot):
        self.S, self.T = src, tgt
        self.sections: list[Section] = []
        self.warnings: list[str] = []
        self.recreated: set[str] = set()  # objects dropped and created again (lose grants and comments)
        self.recomment: set[str] = set()  # objects whose comments must be re-applied

    def sec(self, title: str) -> Section:
        s = Section(title)
        self.sections.append(s)
        return s

    # ---------------------------------------------------------------- build
    def build(self) -> None:  # noqa: C901 - a long but linear recipe
        S, T, q = self.S, self.T, quote_ident

        # ---- what changes
        rebuilt = {n for n in set(S.tables) & set(T.tables)
                   if (S.tables[n]["relation_type"], S.tables[n]["external_file"])
                   != (T.tables[n]["relation_type"], T.tables[n]["external_file"])}
        for n in sorted(rebuilt):
            self.warnings.append(f"Table {n} changes its kind and is recreated: its data is lost")
        tables_drop = (set(T.tables) - set(S.tables)) | rebuilt
        tables_new = (set(S.tables) - set(T.tables)) | rebuilt
        col_plans = {n: self._plan_columns(n) for n in sorted(set(S.tables) & set(T.tables) - rebuilt)}
        col_touched = {n for n, p in col_plans.items() if p["drop"] or p["type_changed"]}
        col_rebuild = {n for n, p in col_plans.items() if p["rebuild"]}

        def changed(kind):
            a, b = getattr(S, kind), getattr(T, kind)
            return {n for n in set(a) & set(b) if a[n]["create"] != b[n]["create"]}

        p_drop = set(T.procedures) - set(S.procedures)
        f_drop = set(T.functions) - set(S.functions)
        p_new = set(S.procedures) - set(T.procedures)
        f_new = set(S.functions) - set(T.functions)
        p_changed, f_changed = changed("procedures"), changed("functions")
        pk_drop = set(T.packages) - set(S.packages)
        pk_new = set(S.packages) - set(T.packages)
        pk_header = {n for n in set(S.packages) & set(T.packages) if S.packages[n]["header"] != T.packages[n]["header"]}
        pk_body = {n for n in set(S.packages) & set(T.packages) if S.packages[n]["body"] != T.packages[n]["body"]}
        tr_drop = set(T.triggers) - set(S.triggers)
        tr_new = set(S.triggers) - set(T.triggers)
        tr_changed = {n for n in set(S.triggers) & set(T.triggers) if S.triggers[n]["ddl"] != T.triggers[n]["ddl"]}
        tr_moved = {n for n in tr_changed if S.triggers[n]["relation"] != T.triggers[n]["relation"]}

        touched = (tables_drop | col_touched | p_drop | f_drop | p_changed | f_changed | pk_drop | pk_header
                   | (set(T.exceptions) - set(S.exceptions)) | (set(T.sequences) - set(S.sequences))
                   | (set(T.domains) - set(S.domains)) | (set(T.udfs) - set(S.udfs)))

        # views to drop and create again
        v_drop_only = set(T.views) - set(S.views)
        v_changed = {n for n in set(S.views) & set(T.views) if S.views[n]["ddl"] != T.views[n]["ddl"]}
        recreate = v_drop_only | {n for n in v_changed if S.views[n]["columns"] != T.views[n]["columns"]}
        recreate |= {d["dep"] for d in T.deps if d["dtype"] == 1 and d["name"] in touched and d["dep"] in T.views}
        grow = True
        while grow:
            more = {d["dep"] for d in T.deps if d["dtype"] == 1 and d["name"] in recreate and d["dep"] in T.views}
            grow = bool(more - recreate)
            recreate |= more
        v_alter = v_changed - recreate
        touched |= recreate

        # PSQL that must be detached
        affected = {d["dep"] for d in T.deps if d["dtype"] in _PSQL_DEP_TYPES and d["name"] in touched}
        affected |= {n for n, t in T.triggers.items() if t["relation"] in (recreate | tables_drop)}
        tr_detach = (tr_drop | tr_moved | (affected & set(T.triggers))) & set(T.triggers)
        body_detach = {n for n in (affected | pk_header | pk_drop | pk_body) & set(T.packages)
                       if T.packages[n]["body"]}
        routine_stub = [(k, n) for k in ("functions", "procedures") for n in sorted(getattr(T, k))
                        if (n in affected or n in (f_drop if k == "functions" else p_drop))
                        and getattr(T, k)[n]["stub_alter"]]

        # ---- 1. revoke and uncomment
        s_grants = {g["ddl"] for g in S.grants}
        t_grants = {g["ddl"] for g in T.grants}
        sec = self.sec("Revoke privileges")
        for g in T.grants:
            if g["ddl"] not in s_grants:
                sec.add(g["revoke"])
        sec = self.sec("Remove comments")
        for key in sorted(set(T.comments) - set(S.comments)):
            sec.add(f"{key} IS NULL")

        # ---- 2. detach PSQL
        sec = self.sec("Drop triggers that are removed or use changed objects")
        for n in sorted(tr_detach):
            sec.add(f"DROP TRIGGER {q(n)}")
        sec = self.sec("Drop package bodies that use changed objects")
        for n in sorted(body_detach):
            sec.add(f"DROP PACKAGE BODY {q(n)}")
        sec = self.sec("Replace bodies of affected procedures and functions with stubs")
        for k, n in routine_stub:
            sec.add(getattr(T, k)[n]["stub_alter"], True)

        # ---- 3. drops
        sec = self.sec("Drop views")
        for n in reversed(T.view_order):
            if n in recreate:
                sec.add(f"DROP VIEW {q(n)}")

        fk_drop: list[tuple[str, dict]] = []
        fk_add: list[tuple[str, dict]] = []
        pkuk_drop, pkuk_add, chk_drop, chk_add = [], [], [], []
        key_dropped: set[tuple[str, tuple]] = set()  # (table, key columns) of dropped primary/unique keys
        for n in col_plans:
            st, tt = S.tables[n], T.tables[n]
            for kind, dl, al in (("pkuk", pkuk_drop, pkuk_add), ("checks", chk_drop, chk_add)):
                sk = {c["key"]: c for c in st[kind]}
                tk = {c["key"]: c for c in tt[kind]}
                rb = n in col_rebuild  # a column is rebuilt: recreate every key and check of the table
                for k, c in tk.items():
                    if rb or k not in sk or sk[k]["ddl"] != c["ddl"]:
                        dl.append((n, c))
                        if kind == "pkuk":
                            key_dropped.add((n, tuple(c["columns"])))
                for k, c in sk.items():
                    if rb or k not in tk or tk[k]["ddl"] != c["ddl"]:
                        al.append((n, c))
        for n in sorted(set(T.tables) - tables_drop):
            sfk = {c["key"]: c for c in S.tables[n]["fks"]}
            for c in T.tables[n]["fks"]:
                s_c = sfk.get(c["key"])
                if (s_c is None or s_c["ddl"] != c["ddl"] or c["ref_table"] in tables_drop
                        or (c["ref_table"], tuple(c["ref_columns"] or ())) in key_dropped or n in col_rebuild):
                    fk_drop.append((n, c))
                    if s_c is not None:
                        fk_add.append((n, s_c))
            tfk = {c["key"] for c in T.tables[n]["fks"]}
            fk_add += [(n, c) for c in S.tables[n]["fks"] if c["key"] not in tfk]
        for n in sorted(tables_new):
            fk_add += [(n, c) for c in S.tables[n]["fks"]]
        fk_add = list({(n, c["ddl"]): (n, c) for n, c in fk_add}.values())

        sec = self.sec("Drop foreign keys")
        for n, c in fk_drop:
            sec.add(f"ALTER TABLE {q(n)} DROP CONSTRAINT {q(c['name'])}")

        idx_drop, idx_create, idx_toggle = [], [], []
        for n, i in T.indices.items():
            s_i = S.indices.get(n)
            if i["table"] in tables_drop:
                if s_i:
                    idx_create.append(n)
                continue
            if (s_i is None or s_i["create"] != i["create"] or s_i["table"] != i["table"]
                    or i["table"] in col_rebuild):
                idx_drop.append(n)
                if s_i:
                    idx_create.append(n)
            elif s_i["inactive"] != i["inactive"]:
                idx_toggle.append(n)
        idx_create += [n for n in S.indices if n not in T.indices]
        sec = self.sec("Drop indices")
        for n in sorted(idx_drop):
            sec.add(f"DROP INDEX {q(n)}")

        sec = self.sec("Drop constraints")
        for n, c in chk_drop + pkuk_drop:
            sec.add(f"ALTER TABLE {q(n)} DROP CONSTRAINT {q(c['name'])}")
        sec = self.sec("Drop columns")
        for n, plan in col_plans.items():
            for c in plan["drop"]:
                sec.add(f"ALTER TABLE {q(n)} DROP {q(c)}")
        sec = self.sec("Drop tables")
        for n in sorted(tables_drop):
            sec.add(f"DROP TABLE {q(n)}")
        self.recreated |= rebuilt

        # ---- 4. create / alter
        sec = self.sec("Collations")
        for n in sorted(set(S.collations) - set(T.collations)):
            sec.add(S.collations[n]["ddl"])
        for n in sorted(set(S.collations) & set(T.collations)):
            if S.collations[n]["ddl"] != T.collations[n]["ddl"]:
                self.warnings.append(f"Collation {n} differs and cannot be altered")
        sec = self.sec("Roles")
        for n in sorted(set(S.roles) - set(T.roles)):
            sec.add(S.roles[n]["ddl"])
        sec = self.sec("Domains")
        for n in sorted(S.domains):
            if n not in T.domains:
                sec.add(S.domains[n]["ddl"])
            elif S.domains[n]["ddl"] != T.domains[n]["ddl"]:
                for stmt in self._alter_domain(n, S.domains[n], T.domains[n]):
                    sec.add(stmt)
        sec = self.sec("External functions")
        for n in sorted(S.udfs):
            if n not in T.udfs:
                sec.add(S.udfs[n]["ddl"])
            elif S.udfs[n]["ddl"] != T.udfs[n]["ddl"]:
                sec.add(f"DROP EXTERNAL FUNCTION {q(n)}")
                sec.add(S.udfs[n]["ddl"])
        sec = self.sec("Sequences")
        for n in sorted(S.sequences):
            if n not in T.sequences:
                sec.add(S.sequences[n]["ddl"])
            elif S.sequences[n]["ddl"] != T.sequences[n]["ddl"]:
                if S.sequences[n]["increment"] != T.sequences[n]["increment"]:
                    sec.add(f"ALTER SEQUENCE {q(n)} INCREMENT BY {S.sequences[n]['increment']}")
                if S.sequences[n]["ddl"].split(" INCREMENT")[0] != T.sequences[n]["ddl"].split(" INCREMENT")[0]:
                    self.warnings.append(f"Sequence {n}: the START WITH value differs (not changed)")
        sec = self.sec("Exceptions")
        for n in sorted(S.exceptions):
            if n not in T.exceptions:
                sec.add(S.exceptions[n]["ddl"])
            elif S.exceptions[n]["ddl"] != T.exceptions[n]["ddl"]:
                sec.add("CREATE OR ALTER" + S.exceptions[n]["ddl"][len("CREATE"):])
        sec = self.sec("Tables")
        for n in sorted(tables_new):
            sec.add(S.tables[n]["create_placeholder"])
        deferred: list[str] = [d for n in sorted(tables_new) for d in S.tables[n]["deferred"]]
        sec = self.sec("Alter tables")
        for plan in col_plans.values():
            for stmt in plan["alter"]:
                sec.add(stmt)
            deferred += plan["deferred"]
        sec = self.sec("Primary and unique keys")
        for n, c in pkuk_add:
            sec.add(f"ALTER TABLE {q(n)} ADD {c['ddl']}")
        sec = self.sec("Foreign keys")
        for n, c in fk_add:
            sec.add(f"ALTER TABLE {q(n)} ADD {c['ddl']}")

        sec = self.sec("New procedures and functions (empty bodies)")
        for k, new in (("functions", f_new), ("procedures", p_new)):
            for n in sorted(new):
                sec.add(getattr(S, k)[n]["stub"], True)
        sec = self.sec("Package headers")
        for n in sorted(pk_new):
            sec.add(S.packages[n]["header"], True)
        for n in sorted(pk_header):
            sec.add(S.packages[n]["header_cor"], True)

        sec = self.sec("Views")
        for n in S.view_order:
            if n not in T.views or n in recreate:
                sec.add(S.views[n]["ddl"])
            elif n in v_alter:
                sec.add(S.views[n]["create_or_alter"])
        self.recreated |= recreate & set(S.views)
        self.recomment |= v_alter

        sec = self.sec("Procedure and function bodies")
        for k, new, chg in (("functions", f_new, f_changed), ("procedures", p_new, p_changed)):
            objs = getattr(S, k)
            for n in sorted((new | chg | affected | {x for kk, x in routine_stub if kk == k}) & set(objs)):
                if n in new and objs[n]["external"]:
                    continue  # created complete above
                sec.add(objs[n]["create_or_alter"], True)
                self.recomment.add(n)
        sec = self.sec("Package bodies")
        for n in sorted((pk_new | pk_header | pk_body | body_detach) & set(S.packages)):
            if S.packages[n]["body"]:
                sec.add("RECREATE" + S.packages[n]["body"][len("CREATE"):], True)
            self.recomment.add(n)

        sec = self.sec("Computed columns")
        for d in deferred:
            sec.add(d)
        sec = self.sec("Check constraints")
        for n in sorted(tables_new):
            chk_add += [(n, c) for c in S.tables[n]["checks"]]
        for n, c in chk_add:
            sec.add(f"ALTER TABLE {q(n)} ADD {c['ddl']}")
        sec = self.sec("Indices")
        for n in sorted(set(idx_create)):
            for stmt in S.indices[n]["ddl"]:
                sec.add(stmt)
            self.recomment.add(n)
        for n in sorted(idx_toggle):
            sec.add(f"ALTER INDEX {q(n)} {'INACTIVE' if S.indices[n]['inactive'] else 'ACTIVE'}")
        sec = self.sec("Triggers")
        on_recreated = {n for n, t in S.triggers.items() if t["relation"] in (recreate | tables_drop)}
        for n in sorted((tr_new | tr_changed | (affected & set(S.triggers)) | tr_detach | on_recreated)
                        & set(S.triggers)):
            sec.add(S.triggers[n]["create_or_alter"], True)
            self.recomment.add(n)

        # ---- 5. drop removed objects
        sec = self.sec("Drop removed objects")
        for n in sorted(p_drop):
            sec.add(f"DROP PROCEDURE {q(n)}")
        for n in sorted(f_drop):
            sec.add(f"DROP FUNCTION {q(n)}")
        for n in sorted(pk_drop):
            sec.add(f"DROP PACKAGE {q(n)}")
        for kind, kw in (("udfs", "EXTERNAL FUNCTION"), ("exceptions", "EXCEPTION"), ("sequences", "SEQUENCE"),
                         ("domains", "DOMAIN"), ("collations", "COLLATION"), ("roles", "ROLE")):
            for n in sorted(set(getattr(T, kind)) - set(getattr(S, kind))):
                sec.add(f"DROP {kw} {q(n)}")

        # ---- 6. grants and comments
        sec = self.sec("Grant privileges")
        for g in S.grants:
            if g["ddl"] not in t_grants or g["object"] in self.recreated:
                sec.add(g["ddl"])
        sec = self.sec("Comments")
        names = (self.recreated | self.recomment | set(tables_new)
                 | {n for n, p in col_plans.items() if p["drop"] or p["rebuild"]})
        for key, stmt in sorted(S.comments.items()):
            if T.comments.get(key) != stmt or _comment_object(key) in names:
                sec.add(stmt)

    # ------------------------------------------------------------ columns
    def _plan_columns(self, name: str) -> dict:
        q = quote_ident
        st, tt = self.S.tables[name], self.T.tables[name]
        scols = {c["name"]: c for c in st["columns"]}
        tcols = {c["name"]: c for c in tt["columns"]}
        s_deferred = set(st["deferred_cols"])
        drop, add, alter, deferred, rebuild = [], [], [], [], []
        type_changed = False
        qt = q(name)
        for cn in tcols:
            if cn not in scols:
                drop.append(cn)
        for cn, sc in scols.items():
            tc = tcols.get(cn)
            if tc is None:
                add.append(cn)
                continue
            if sc["fragment"] == tc["fragment"]:
                continue
            if bool(sc["computed"]) != bool(tc["computed"]) or sc["identity"] != tc["identity"]:
                self.warnings.append(f"Column {name}.{cn} is recreated: its data is lost")
                drop.append(cn)
                add.append(cn)
                continue
            if sc["computed"]:
                deferred.append(f"ALTER TABLE {qt} ALTER {q(cn)} COMPUTED BY {sc['computed']}")
                type_changed = True
                continue
            s_type = q(sc["domain"]) if sc["domain"] else sc["data_type"]
            t_type = q(tc["domain"]) if tc["domain"] else tc["data_type"]
            if s_type != t_type or sc["collation"] != tc["collation"]:
                type_changed = True
                if sc["collation"] != tc["collation"] or not _widening(t_type, s_type):
                    # Firebird cannot narrow or convert in place: copy through a new column
                    self.warnings.append(f"Column {name}.{cn} is rebuilt to change its type (values are copied)")
                    tmp = q("FBADMIN$TMP")
                    alter += [f"ALTER TABLE {qt} ADD {tmp} " + column_fragment({**sc, "name": "X"})[2:],
                              f"UPDATE {qt} SET {tmp} = {q(cn)}",
                              f"ALTER TABLE {qt} DROP {q(cn)}",
                              f"ALTER TABLE {qt} ALTER {tmp} TO {q(cn)}"]
                    rebuild.append(cn)
                    continue
                alter.append(f"ALTER TABLE {qt} ALTER {q(cn)} TYPE {s_type}")
            if (sc["col_default"] or None) != (tc["col_default"] or None):
                if sc["col_default"]:
                    dflt = sc["col_default"]
                    dflt = dflt[len("DEFAULT"):].strip() if dflt.upper().startswith("DEFAULT") else dflt
                    alter.append(f"ALTER TABLE {qt} ALTER {q(cn)} SET DEFAULT {dflt}")
                else:
                    alter.append(f"ALTER TABLE {qt} ALTER {q(cn)} DROP DEFAULT")
            if sc["col_not_null"] != tc["col_not_null"]:
                alter.append(f"ALTER TABLE {qt} ALTER {q(cn)} {'SET' if sc['col_not_null'] else 'DROP'} NOT NULL")
            if (sc["identity_start"], sc["identity_increment"]) != (tc["identity_start"], tc["identity_increment"]):
                self.warnings.append(f"Identity options of {name}.{cn} differ (not changed)")
        adds = []
        for cn in add:
            sc = scols[cn]
            if cn in s_deferred:
                adds.append(f"ALTER TABLE {qt} ADD {column_fragment(sc, placeholder=True)}")
                deferred.append(f"ALTER TABLE {qt} ALTER {q(cn)} COMPUTED BY {sc['computed']}")
            else:
                adds.append(f"ALTER TABLE {qt} ADD {column_fragment(sc)}")
        # column order after the changes; fix positions if needed
        order = ([c["name"] for c in tt["columns"] if c["name"] not in drop and c["name"] not in rebuild]
                 + rebuild + [c for c in scols if c in add])
        wanted = [c["name"] for c in st["columns"]]
        positions = []
        if order != wanted:
            first = next(i for i, (a, b) in enumerate(zip(order, wanted, strict=False)) if a != b)
            positions = [f"ALTER TABLE {qt} ALTER {q(cn)} POSITION {i + 1}"
                         for i, cn in enumerate(wanted) if i >= first]
        return {"drop": drop, "alter": adds + alter + positions, "deferred": deferred, "type_changed": type_changed,
                "rebuild": rebuild}

    def _alter_domain(self, name: str, s: dict, t: dict) -> list[str]:
        q = quote_ident(name)
        out = []
        if s["type"] != t["type"]:
            out.append(f"ALTER DOMAIN {q} TYPE {s['type']}")
        if s["default"] != t["default"]:
            if s["default"]:
                d = s["default"]
                d = d[len("DEFAULT"):].strip() if d.upper().startswith("DEFAULT") else d
                out.append(f"ALTER DOMAIN {q} SET DEFAULT {d}")
            else:
                out.append(f"ALTER DOMAIN {q} DROP DEFAULT")
        if bool(s["not_null"]) != bool(t["not_null"]):
            out.append(f"ALTER DOMAIN {q} {'SET' if s['not_null'] else 'DROP'} NOT NULL")
        if s["check"] != t["check"]:
            if t["check"]:
                out.append(f"ALTER DOMAIN {q} DROP CONSTRAINT")
            if s["check"]:
                out.append(f"ALTER DOMAIN {q} ADD {s['check']}")
        if s["collation"] != t["collation"]:
            self.warnings.append(f"Domain {name}: the collation differs and cannot be altered")
        return out

    def script(self, set_term: bool = True, header: str | None = None) -> str:
        secs = [s for s in self.sections if s.statements]
        if not secs:
            return (header or "") + "/* The schemas are identical: nothing to do */\n"
        head = header or ""
        if self.warnings:
            head += "/* Warnings:\n" + "\n".join(f"   - {w}" for w in self.warnings) + "\n*/\n"
        return render(secs, set_term=set_term, header=head or None)


def compare(src: Snapshot, tgt: Snapshot, *, set_term: bool = True, header: str | None = None) -> dict[str, Any]:
    items = compare_objects(src, tgt)
    mig = Migration(src, tgt)
    if items:
        mig.build()
    summary = {k: sum(1 for i in items if i["status"] == k) for k in ("create", "alter", "drop")}
    return {
        "items": items,
        "summary": summary,
        "warnings": mig.warnings,
        "script": mig.script(set_term=set_term, header=header),
    }

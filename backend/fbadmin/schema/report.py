"""Unused or invalid objects report."""

from __future__ import annotations

from typing import Any

from ..metadata import Catalog


def objects_report(cat: Catalog) -> list[dict[str, Any]]:
    secs: list[dict[str, Any]] = []

    def add(sid, title, description, otype, rows, detail=None):
        secs.append({
            "id": sid, "title": title, "description": description,
            "items": [{"type": r.get("otype", otype), "name": r["name"], "parent": r.get("parent"),
                       "detail": detail(r) if detail else r.get("detail")} for r in rows],
        })

    add("invalid_procedures", "Invalid procedures", "RDB$VALID_BLR = 0: must be recompiled (ALTER) to run",
        "procedures", cat.rows(
            "SELECT TRIM(RDB$PROCEDURE_NAME) AS name, TRIM(RDB$PACKAGE_NAME) AS parent FROM RDB$PROCEDURES "
            "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 ORDER BY 1"),
        lambda r: f"in package {r['parent']}" if r["parent"] else None)
    add("invalid_functions", "Invalid functions", "RDB$VALID_BLR = 0", "functions", cat.rows(
        "SELECT TRIM(RDB$FUNCTION_NAME) AS name, TRIM(RDB$PACKAGE_NAME) AS parent FROM RDB$FUNCTIONS "
        "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 ORDER BY 1"),
        lambda r: f"in package {r['parent']}" if r["parent"] else None)
    add("invalid_triggers", "Invalid triggers", "RDB$VALID_BLR = 0", "triggers", cat.rows(
        "SELECT TRIM(RDB$TRIGGER_NAME) AS name, TRIM(RDB$RELATION_NAME) AS parent FROM RDB$TRIGGERS "
        "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VALID_BLR = 0 ORDER BY 1"))
    add("invalid_packages", "Invalid package bodies",
        "The body must be recreated (usually after the package header changed)", "packages", cat.rows(
            "SELECT TRIM(RDB$PACKAGE_NAME) AS name FROM RDB$PACKAGES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
            "AND RDB$PACKAGE_BODY_SOURCE IS NOT NULL AND COALESCE(RDB$VALID_BODY_FLAG, 0) = 0 ORDER BY 1"))
    add("packages_without_body", "Packages without a body", "Calling their routines fails", "packages", cat.rows(
        "SELECT TRIM(RDB$PACKAGE_NAME) AS name FROM RDB$PACKAGES WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 "
        "AND RDB$PACKAGE_BODY_SOURCE IS NULL ORDER BY 1"))
    add("inactive_indices", "Inactive indices", "Not maintained and not used by the optimizer", "indices", cat.rows(
        "SELECT TRIM(i.RDB$INDEX_NAME) AS name, TRIM(i.RDB$RELATION_NAME) AS parent FROM RDB$INDICES i "
        "JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = i.RDB$RELATION_NAME "
        "WHERE COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND i.RDB$INDEX_INACTIVE = 1 ORDER BY 1"),
        lambda r: f"on {r['parent']}")
    add("inactive_triggers", "Inactive triggers", "", "triggers", cat.rows(
        "SELECT TRIM(RDB$TRIGGER_NAME) AS name, TRIM(RDB$RELATION_NAME) AS parent FROM RDB$TRIGGERS "
        "WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$TRIGGER_INACTIVE = 1 ORDER BY 1"),
        lambda r: f"on {r['parent']}" if r["parent"] else "database trigger")
    add("unused_domains", "Unused domains", "Not used by any column, parameter or PSQL variable", "domains", cat.rows(
        "SELECT TRIM(f.RDB$FIELD_NAME) AS name FROM RDB$FIELDS f WHERE f.RDB$FIELD_NAME NOT STARTING WITH 'RDB$' "
        "AND COALESCE(f.RDB$SYSTEM_FLAG, 0) = 0 "
        "AND NOT EXISTS (SELECT 1 FROM RDB$RELATION_FIELDS rf WHERE rf.RDB$FIELD_SOURCE = f.RDB$FIELD_NAME) "
        "AND NOT EXISTS (SELECT 1 FROM RDB$PROCEDURE_PARAMETERS pp WHERE pp.RDB$FIELD_SOURCE = f.RDB$FIELD_NAME) "
        "AND NOT EXISTS (SELECT 1 FROM RDB$FUNCTION_ARGUMENTS fa WHERE fa.RDB$FIELD_SOURCE = f.RDB$FIELD_NAME) "
        "AND NOT EXISTS (SELECT 1 FROM RDB$DEPENDENCIES d WHERE d.RDB$DEPENDED_ON_NAME = f.RDB$FIELD_NAME "
        "AND d.RDB$DEPENDED_ON_TYPE = 9) ORDER BY 1"))
    add("unused_exceptions", "Unused exceptions", "Not raised by any PSQL module", "exceptions", cat.rows(
        "SELECT TRIM(e.RDB$EXCEPTION_NAME) AS name FROM RDB$EXCEPTIONS e WHERE COALESCE(e.RDB$SYSTEM_FLAG, 0) = 0 "
        "AND NOT EXISTS (SELECT 1 FROM RDB$DEPENDENCIES d WHERE d.RDB$DEPENDED_ON_NAME = e.RDB$EXCEPTION_NAME "
        "AND d.RDB$DEPENDED_ON_TYPE = 7) ORDER BY 1"))
    add("unused_sequences", "Unused sequences", "Not used by any PSQL module, view or default (identity "
        "sequences are excluded)", "sequences", cat.rows(
            "SELECT TRIM(g.RDB$GENERATOR_NAME) AS name FROM RDB$GENERATORS g "
            "WHERE COALESCE(g.RDB$SYSTEM_FLAG, 0) = 0 "
            "AND NOT EXISTS (SELECT 1 FROM RDB$DEPENDENCIES d WHERE d.RDB$DEPENDED_ON_NAME = g.RDB$GENERATOR_NAME "
            "AND d.RDB$DEPENDED_ON_TYPE = 14) ORDER BY 1"))
    add("tables_without_pk", "Tables without a primary key", "Rows can only be identified by RDB$DB_KEY",
        "tables", cat.rows(
            "SELECT TRIM(r.RDB$RELATION_NAME) AS name FROM RDB$RELATIONS r WHERE r.RDB$VIEW_BLR IS NULL "
            "AND COALESCE(r.RDB$SYSTEM_FLAG, 0) = 0 AND COALESCE(r.RDB$RELATION_TYPE, 0) = 0 "
            "AND NOT EXISTS (SELECT 1 FROM RDB$RELATION_CONSTRAINTS rc WHERE rc.RDB$RELATION_NAME = "
            "r.RDB$RELATION_NAME AND rc.RDB$CONSTRAINT_TYPE = 'PRIMARY KEY') ORDER BY 1"))
    return secs

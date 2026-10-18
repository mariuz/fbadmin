"""Firebird data type decoding and identifier quoting."""

from __future__ import annotations

import re

_BASE_TYPES = {
    7: "SMALLINT",
    8: "INTEGER",
    10: "FLOAT",
    12: "DATE",
    13: "TIME",
    16: "BIGINT",
    23: "BOOLEAN",
    24: "DECFLOAT(16)",
    25: "DECFLOAT(34)",
    26: "INT128",
    27: "DOUBLE PRECISION",
    28: "TIME WITH TIME ZONE",
    29: "TIMESTAMP WITH TIME ZONE",
    35: "TIMESTAMP",
}
_DEFAULT_PRECISION = {7: 4, 8: 9, 16: 18, 26: 38, 27: 15}
_BLOB_SUBTYPES = {0: "BINARY", 1: "TEXT", 2: "BLR", 3: "ACL"}


def format_type(
    ftype: int | None,
    subtype: int | None = 0,
    length: int | None = None,
    precision: int | None = None,
    scale: int | None = 0,
    char_length: int | None = None,
    segment_length: int | None = None,
    charset: str | None = None,
    default_charset: str | None = None,
) -> str:
    """Return the SQL type declaration for a row of RDB$FIELDS."""
    if ftype is None:
        return "UNKNOWN"
    subtype = subtype or 0
    scale = scale or 0
    cs = ""
    if charset and charset.strip() and charset != default_charset:
        cs = f" CHARACTER SET {charset.strip()}"

    if ftype in (7, 8, 16, 26, 27) and (subtype in (1, 2) or scale < 0):
        kind = "DECIMAL" if subtype == 2 else "NUMERIC"
        prec = precision or _DEFAULT_PRECISION.get(ftype, 18)
        return f"{kind}({prec},{-scale})"
    if ftype == 14:
        return f"CHAR({char_length or length}){cs}"
    if ftype == 37:
        return f"VARCHAR({char_length or length}){cs}"
    if ftype == 40:
        return f"CSTRING({length})"
    if ftype == 261:
        st = _BLOB_SUBTYPES.get(subtype, str(subtype))
        out = f"BLOB SUB_TYPE {st}"
        if segment_length and segment_length != 80:
            out += f" SEGMENT SIZE {segment_length}"
        if subtype == 1:
            out += cs
        return out
    if ftype == 10 and precision:
        return f"FLOAT({precision})"
    return _BASE_TYPES.get(ftype, f"TYPE_{ftype}")


# Reserved words that force quoting of identifiers (Firebird 5 list, abridged to
# words that are plausible as column/table names plus all core keywords).
RESERVED = frozenset(
    """ADD ADMIN ALL ALTER AND ANY AS AT AVG BEGIN BETWEEN BIGINT BINARY BIT_LENGTH BLOB BOOLEAN BOTH BY
    CASE CAST CHAR CHARACTER CHARACTER_LENGTH CHAR_LENGTH CHECK CLOSE COLLATE COLUMN COMMENT COMMIT CONNECT
    CONSTRAINT CORR COUNT COVAR_POP COVAR_SAMP CREATE CROSS CURRENT CURRENT_CONNECTION CURRENT_DATE CURRENT_ROLE
    CURRENT_TIME CURRENT_TIMESTAMP CURRENT_TRANSACTION CURRENT_USER CURSOR DATE DAY DEC DECFLOAT DECIMAL DECLARE
    DEFAULT DELETE DELETING DETERMINISTIC DISCONNECT DISTINCT DOUBLE DROP ELSE END ESCAPE EXECUTE EXISTS EXTERNAL
    EXTRACT FALSE FETCH FILTER FLOAT FOR FOREIGN FROM FULL FUNCTION GDSCODE GLOBAL GRANT GROUP HAVING HOUR IN
    INDEX INNER INSENSITIVE INSERT INSERTING INT INT128 INTEGER INTO IS JOIN LATERAL LEADING LEFT LIKE LOCAL
    LOCALTIME LOCALTIMESTAMP LONG LOWER MAX MERGE MIN MINUTE MONTH NATIONAL NATURAL NCHAR NO NOT NULL NUMERIC
    OCTET_LENGTH OF OFFSET ON ONLY OPEN OR ORDER OUTER OVER PARAMETER PLAN POSITION POST_EVENT PRECISION
    PRIMARY PROCEDURE PUBLICATION RDB$DB_KEY RDB$ERROR RDB$GET_CONTEXT RDB$GET_TRANSACTION_CN RDB$RECORD_VERSION
    RDB$ROLE_IN_USE RDB$SET_CONTEXT RDB$SYSTEM_PRIVILEGE REAL RECORD_VERSION RECREATE RECURSIVE REFERENCES
    REGR_AVGX REGR_AVGY REGR_COUNT REGR_INTERCEPT REGR_R2 REGR_SLOPE REGR_SXX REGR_SXY REGR_SYY RELEASE
    RESETTING RETURN RETURNING_VALUES RETURNS REVOKE RIGHT ROLLBACK ROW ROWS ROW_COUNT SAVEPOINT SCROLL SECOND
    SELECT SENSITIVE SET SIMILAR SMALLINT SOME SQLCODE SQLSTATE START STDDEV_POP STDDEV_SAMP SUM TABLE THEN
    TIME TIMESTAMP TIMEZONE_HOUR TIMEZONE_MINUTE TO TRAILING TRIGGER TRIM TRUE UNBOUNDED UNION UNIQUE UNKNOWN
    UPDATE UPDATING UPPER USER USING VALUE VALUES VARBINARY VARCHAR VARIABLE VARYING VAR_POP VAR_SAMP VIEW
    WHEN WHERE WHILE WINDOW WITH WITHOUT YEAR""".split()
)

_PLAIN = re.compile(r"^[A-Z][A-Z0-9_$]*$")


def quote_ident(name: str) -> str:
    """Quote an identifier only when needed (dialect 3)."""
    if name is None:
        return ""
    name = name.rstrip()
    if _PLAIN.match(name) and name not in RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def sql_string(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"

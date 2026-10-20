"""SQL literals for the data sync script (Firebird 3, 4, 5 and 6, dialect 3).

Every value is written as a literal, so the script runs unchanged in isql, in fbadmin's Query Tool
and in the "Apply to target" job. ``kind`` is a hint derived from the target column (see
``engine.column_kind``):

* ``decfloat``: ``CAST('1.5E+300' AS DECFLOAT(34))`` (also NaN/Infinity, huge exponents)
* ``time_tz`` / ``timestamp_tz``: the value is the server's own text form (the columns are
  selected as ``CAST(.. AS VARCHAR)`` because the driver loses the zone), e.g.
  ``TIMESTAMP '2024-03-01 10:20:30.1234 Europe/Bucharest'``
* ``blob_text`` / ``blob``: long values are split into chunks joined with ``||``, the first one
  cast to a BLOB so the concatenation is not limited to 32 KB
* anything else: chosen from the Python type (int, Decimal, float, bool, str, bytes, date, time,
  datetime).

String literals are limited by Firebird to 65,535 bytes (16,383 characters in UTF8), hence the
chunking of long text; binary values use ``x'..'`` hex literals.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import re
from typing import Any

TEXT_CHUNK = 16000  # characters per string literal (UTF8: at most 16,383 characters)
BIN_CHUNK = 65000  # bytes per x'..' literal (at most 65,535)
# Line breaks and other control characters inside a quoted literal do not survive every tool
# (isql doubles line breaks in multi-line literals, editors change CR/LF), so text containing
# them is written as a UTF-8 hex literal with an introducer: _UTF8 x'..'.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _ftime(t: dt.time | dt.datetime) -> str:
    """hh:mm:ss[.ffff] - Firebird stores 1/10000 of a second."""
    s = f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}"
    frac = t.microsecond // 100
    if frac:
        s += f".{frac:04d}"
    return s


def _fdate(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _zone(t: dt.time | dt.datetime) -> str:
    """Zone of an aware value: region name (ZoneInfo) or a +hh:mm offset."""
    key = getattr(t.tzinfo, "key", None)
    if key:
        return " " + key
    off = t.utcoffset() if isinstance(t, dt.datetime) else t.tzinfo.utcoffset(None)
    if off is None:
        return ""
    minutes = int(off.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return f" {sign}{minutes // 60:02d}:{minutes % 60:02d}"


def string_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _join(parts: list[str], blob_type: str | None) -> str:
    if len(parts) == 1:
        return parts[0]
    first = f"CAST({parts[0]} AS {blob_type})" if blob_type else parts[0]
    return " || ".join([first, *parts[1:]])


def text_literal(s: str, kind: str = "") -> str:
    if _CONTROL.search(s):
        parts = [f"_UTF8 x'{s[i:i + TEXT_CHUNK].encode('utf-8').hex().upper()}'" for i in range(0, len(s), TEXT_CHUNK)]
        blob = "BLOB SUB_TYPE TEXT CHARACTER SET UTF8"
    else:
        parts = [string_literal(s[i:i + TEXT_CHUNK]) for i in range(0, len(s), TEXT_CHUNK)] or ["''"]
        blob = "BLOB SUB_TYPE TEXT"
    return _join(parts, blob if kind.startswith("blob") else None)


def binary_literal(b: bytes, kind: str = "") -> str:
    if len(b) <= BIN_CHUNK:
        return f"x'{b.hex().upper()}'"
    parts = [f"x'{b[i:i + BIN_CHUNK].hex().upper()}'" for i in range(0, len(b), BIN_CHUNK)]
    return _join(parts, "BLOB SUB_TYPE BINARY" if kind.startswith("blob") else None)


def sql_literal(v: Any, kind: str = "") -> str:
    """A Firebird SQL literal for ``v`` (``kind``: hint from the target column, see module doc)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"The floating point value {v} cannot be written as a Firebird literal")
        return repr(v)
    if isinstance(v, decimal.Decimal):
        if kind == "decfloat" or not v.is_finite():
            return f"CAST('{v}' AS DECFLOAT(34))"
        return format(v, "f")
    if isinstance(v, (bytes, bytearray, memoryview)):
        return binary_literal(bytes(v), kind)
    if isinstance(v, dt.datetime):
        zone = _zone(v) if v.tzinfo else ""
        return f"TIMESTAMP '{_fdate(v)} {_ftime(v)}{zone}'"
    if isinstance(v, dt.date):
        return f"DATE '{_fdate(v)}'"
    if isinstance(v, dt.time):
        zone = _zone(v) if v.tzinfo else ""
        return f"TIME '{_ftime(v)}{zone}'"
    if isinstance(v, str):
        if kind == "time_tz":
            return "TIME " + string_literal(v)
        if kind == "timestamp_tz":
            return "TIMESTAMP " + string_literal(v)
        return text_literal(v, kind)
    raise ValueError(f"Cannot write a SQL literal for a value of type {type(v).__name__}")

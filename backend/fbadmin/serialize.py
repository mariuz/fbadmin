"""Convert Firebird values into JSON-safe values for the browser."""

from __future__ import annotations

import datetime as dt
import decimal
import math
import uuid
from typing import Any

MAX_SAFE_INT = 2**53 - 1
MAX_BINARY_PREVIEW = 65536

TYPE_NAMES = {
    452: "CHAR", 448: "VARCHAR", 500: "SMALLINT", 496: "INTEGER", 482: "FLOAT", 480: "DOUBLE PRECISION",
    530: "D_FLOAT", 510: "TIMESTAMP", 520: "BLOB", 540: "ARRAY", 550: "QUAD", 560: "TIME", 570: "DATE",
    580: "BIGINT", 32752: "INT128", 32754: "TIMESTAMP WITH TIME ZONE", 32756: "TIME WITH TIME ZONE",
    32760: "DECFLOAT(16)", 32762: "DECFLOAT(34)", 32764: "BOOLEAN", 32766: "NULL",
}


def type_name(sqltype: int, scale: int = 0) -> str:
    base = TYPE_NAMES.get(sqltype & ~1, f"TYPE {sqltype}")
    if scale and scale < 0 and base in ("SMALLINT", "INTEGER", "BIGINT", "INT128"):
        return f"NUMERIC(*,{-scale})"
    return base


def to_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INT <= value <= MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        if len(b) == 16:
            return "0x" + b.hex().upper()  # likely a UUID stored as CHAR(16) OCTETS
        if len(b) > MAX_BINARY_PREVIEW:
            return f"<BINARY {len(b)} bytes> 0x" + b[:64].hex().upper() + "..."
        return "0x" + b.hex().upper()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    return str(value)

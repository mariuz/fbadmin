"""Import (CSV, JSON, XLSX -> table) and export (table or query -> CSV, JSON, XLSX, SQL INSERTs).

Uploads are streamed to disk (``<data_dir>/uploads``) and read back as row iterators, so large files
never have to fit in memory (except JSON documents that are a single array, which are parsed
incrementally as well).
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import itertools
import json
import re
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ..config import settings
from ..metadata.types import quote_ident
from ..serialize import to_json

FORMATS = ("csv", "json", "xlsx")


# ------------------------------------------------------------------ uploads
def upload_dir() -> Path:
    d = settings.data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def guess_format(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("xlsx", "xlsm"):
        return "xlsx"
    if ext in ("json", "ndjson", "jsonl"):
        return "json"
    return "csv"


def new_upload_path(filename: str) -> tuple[str, Path]:
    # forget uploads that were never imported
    for old in upload_dir().glob("*__*"):
        try:
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    uid = uuid.uuid4().hex[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-80:] or "upload"
    return uid, upload_dir() / f"{uid}__{safe}"


def upload_path(upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", upload_id or ""):
        raise ValueError("Invalid upload id")
    for p in upload_dir().glob(f"{upload_id}__*"):
        return p
    raise LookupError(f"Upload {upload_id!r} not found (uploads are removed after import)")


def upload_info(upload_id: str) -> dict[str, Any]:
    p = upload_path(upload_id)
    name = p.name.split("__", 1)[1]
    return {"upload_id": upload_id, "filename": name, "size": p.stat().st_size, "format": guess_format(name)}


# ------------------------------------------------------------------ readers
def _json_stream(f, chunk: int = 1 << 16) -> Iterator[Any]:
    """Yield the items of a top-level JSON array (or the lines of NDJSON) without loading everything."""
    dec = json.JSONDecoder()
    buf = f.read(chunk)
    pos = 0
    while pos < len(buf) and buf[pos].isspace():
        pos += 1
    if pos < len(buf) and buf[pos] == "{":
        # an object: either NDJSON or {"data"/"rows": [...]} - small enough to parse as a whole
        rest = buf[pos:] + f.read()
        try:
            doc = json.loads(rest)
        except json.JSONDecodeError:
            for line in rest.splitlines():
                if line.strip():
                    yield json.loads(line)
            return
        for key in ("data", "rows", "items", "records"):
            if isinstance(doc.get(key), list):
                yield from doc[key]
                return
        yield doc
        return
    if pos >= len(buf) or buf[pos] != "[":
        raise ValueError("JSON import expects an array of objects (or of arrays), or NDJSON")
    pos += 1
    while True:
        while True:
            while pos < len(buf) and (buf[pos].isspace() or buf[pos] == ","):
                pos += 1
            if pos < len(buf):
                break
            more = f.read(chunk)
            if not more:
                return
            buf, pos = buf[pos:] + more, 0
        if buf[pos] == "]":
            return
        while True:
            try:
                obj, end = dec.raw_decode(buf, pos)
                break
            except json.JSONDecodeError:
                more = f.read(chunk)
                if not more:
                    raise
                buf, pos = buf[pos:] + more, 0
        yield obj
        pos = end
        if pos > chunk:
            buf, pos = buf[pos:], 0


class Reader:
    """Rows of an uploaded file: ``columns`` (names) and ``rows()`` (iterator of lists)."""

    def __init__(self, path: Path, fmt: str, options: dict | None = None):
        self.path, self.fmt, self.opts = path, fmt, options or {}
        if fmt not in FORMATS:
            raise ValueError(f"Unknown format {fmt!r}")
        self.columns: list[str] = []
        self.sheets: list[str] = []
        self._first: list[list[Any]] = []
        self._it: Iterator[list[Any]] | None = None
        self._open()

    def _open(self) -> None:
        header = self.opts.get("header", True)
        if self.fmt == "csv":
            enc = self.opts.get("encoding") or "utf-8-sig"
            self._fh = open(self.path, encoding=enc, newline="")
            delim = self.opts.get("delimiter") or None
            if not delim:
                sample = self._fh.read(65536)
                self._fh.seek(0)
                try:
                    delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                except csv.Error:
                    delim = ","
            self.delimiter = delim
            it = csv.reader(self._fh, delimiter=delim, quotechar=self.opts.get("quotechar") or '"')
            raw: Iterator[list[Any]] = (r for r in it if r)
        elif self.fmt == "json":
            self._fh = open(self.path, encoding=self.opts.get("encoding") or "utf-8-sig")
            items = _json_stream(self._fh)
            raw = self._json_rows(items)
            header = False
        else:
            from openpyxl import load_workbook

            self._wb = load_workbook(self.path, read_only=True, data_only=True)
            self.sheets = list(self._wb.sheetnames)
            ws = self._wb[self.opts["sheet"]] if self.opts.get("sheet") in self.sheets else self._wb.worksheets[0]
            raw = (list(r) for r in ws.iter_rows(values_only=True) if any(v is not None for v in r))
        if header:
            first = next(raw, None)
            self.columns = [str(c).strip() if c is not None else f"COL{i + 1}" for i, c in enumerate(first or [])]
        else:
            # peek at the first row so the column names are known before rows() starts
            first = next(raw, None)
            if first is not None:
                if not self.columns:
                    self.columns = [f"COL{i + 1}" for i in range(len(first))]
                raw = itertools.chain([first], raw)
        self._it = raw

    def _json_rows(self, items: Iterator[Any]) -> Iterator[list[Any]]:
        first = next(items, None)
        if first is None:
            return
        if isinstance(first, dict):
            # columns: keys of the first objects (more keys found later are ignored)
            self.columns = list(first.keys())
            buffered = [first]
            for _ in range(200):
                nxt = next(items, None)
                if nxt is None:
                    break
                buffered.append(nxt)
                for k in nxt:
                    if k not in self.columns:
                        self.columns.append(k)
            for obj in buffered:
                yield [obj.get(c) for c in self.columns]
            for obj in items:
                yield [obj.get(c) for c in self.columns] if isinstance(obj, dict) else list(obj)
        else:
            first = list(first)
            if self.opts.get("header", True):
                self.columns = [str(c) for c in first]
            else:
                self.columns = [f"COL{i + 1}" for i in range(len(first))]
                yield first
            for row in items:
                yield list(row)

    def rows(self) -> Iterator[list[Any]]:
        assert self._it is not None
        width = len(self.columns)
        for r in self._it:
            if len(r) > width:
                self.columns += [f"COL{i + 1}" for i in range(width, len(r))]
                width = len(r)
            yield list(r) + [None] * (width - len(r))

    def close(self) -> None:
        for attr in ("_fh",):
            f = getattr(self, attr, None)
            if f:
                f.close()
        wb = getattr(self, "_wb", None)
        if wb:
            wb.close()


# --------------------------------------------------------- type inference
_INT = re.compile(r"^[-+]?\d+$")
_DEC = re.compile(r"^[-+]?\d*\.(\d+)$")
_FLOAT = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)[eE][-+]?\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_BOOL = {"true": True, "false": False, "t": True, "f": False, "yes": True, "no": False, "y": True, "n": False}
_VARCHAR_SIZES = [20, 50, 100, 255, 500, 1000, 2000, 4000, 8191]


def infer_type(values: Iterable[Any]) -> str:
    kinds: set[str] = set()
    maxlen, scale, big = 0, 0, False
    for v in values:
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if isinstance(v, bool):
            kinds.add("bool")
        elif isinstance(v, int):
            kinds.add("int")
            big = big or abs(v) > 2**31 - 1
        elif isinstance(v, float):
            kinds.add("float")
        elif isinstance(v, decimal.Decimal):
            kinds.add("dec")
        elif isinstance(v, dt.datetime):
            kinds.add("ts")
        elif isinstance(v, dt.date):
            kinds.add("date")
        elif isinstance(v, dt.time):
            kinds.add("time")
        elif isinstance(v, (dict, list)):
            kinds.add("text")
            maxlen = max(maxlen, len(json.dumps(v)))
        else:
            s = str(v).strip()
            maxlen = max(maxlen, len(str(v)))
            if _INT.match(s) and len(s.lstrip("+-")) <= 18:
                kinds.add("int")
                big = big or abs(int(s)) > 2**31 - 1
            elif m := _DEC.match(s):
                kinds.add("dec")
                scale = max(scale, len(m.group(1)))
            elif _FLOAT.match(s):
                kinds.add("float")
            elif _DATE.match(s):
                kinds.add("date")
            elif _TS.match(s):
                kinds.add("ts")
            elif _TIME.match(s):
                kinds.add("time")
            elif s.lower() in ("true", "false"):
                kinds.add("bool")
            else:
                kinds.add("text")
    if not kinds:
        return "VARCHAR(50)"
    if kinds == {"bool"}:
        return "BOOLEAN"
    if kinds == {"int"}:
        return "BIGINT" if big else "INTEGER"
    if kinds <= {"int", "dec"}:
        return f"NUMERIC(18,{min(max(scale, 2), 8)})"
    if kinds <= {"int", "dec", "float"}:
        return "DOUBLE PRECISION"
    if kinds == {"date"}:
        return "DATE"
    if kinds <= {"date", "ts"}:
        return "TIMESTAMP"
    if kinds == {"time"}:
        return "TIME"
    # headroom: the sample may not contain the longest value of the file
    size = next((s for s in _VARCHAR_SIZES if s >= maxlen * 1.25), None)
    return f"VARCHAR({size})" if size else "BLOB SUB_TYPE TEXT"


def sanitize_name(name: str, used: set[str]) -> str:
    n = re.sub(r"[^A-Za-z0-9_$]", "_", str(name).strip()).upper().strip("_") or "COL"
    if n[0].isdigit():
        n = "C_" + n
    n = n[:31]
    base, i = n, 1
    while n in used:
        i += 1
        n = f"{base[:27]}_{i}"
    used.add(n)
    return n


def preview(path: Path, fmt: str, options: dict | None = None, limit: int = 20, sample: int = 2000) -> dict:
    r = Reader(path, fmt, options)
    try:
        rows: list[list[Any]] = []
        for i, row in enumerate(r.rows()):
            rows.append(row)
            if i + 1 >= sample:
                break
        used: set[str] = set()
        cols = [{"name": c, "suggested_name": sanitize_name(c, used),
                 "type": infer_type(row[i] if i < len(row) else None for row in rows)}
                for i, c in enumerate(r.columns)]
        return {
            "format": fmt, "columns": cols, "rows": [[to_json(v) for v in row] for row in rows[:limit]],
            "sampled_rows": len(rows), "sheets": r.sheets, "delimiter": getattr(r, "delimiter", None),
        }
    finally:
        r.close()


# --------------------------------------------------------- type conversion
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d")


def _parse_date(s: str) -> dt.date:
    s = s.strip()
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        pass
    for f in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {s!r}")


def _parse_ts(s: str) -> dt.datetime:
    s = s.strip().replace("Z", "+00:00")
    try:
        v = dt.datetime.fromisoformat(s)
        return v.replace(tzinfo=None) if v.tzinfo else v
    except ValueError:
        pass
    for f in _DATE_FORMATS:
        for tf in (" %H:%M:%S", " %H:%M", ""):
            try:
                return dt.datetime.strptime(s, f + tf)
            except ValueError:
                continue
    raise ValueError(f"not a timestamp: {s!r}")


def converter(data_type: str, empty_as_null: bool = True):
    """Return a function converting an imported value to what the driver expects for ``data_type``."""
    t = data_type.upper()

    def blank(v):
        return v is None or (isinstance(v, str) and v.strip() == "")

    if t.startswith(("SMALLINT", "INTEGER", "BIGINT", "INT128")):
        def conv(v):
            if blank(v):
                return None
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int,)):
                return v
            if isinstance(v, float) and v.is_integer():
                return int(v)
            s = str(v).strip()
            d = decimal.Decimal(s)
            if d != d.to_integral_value():
                raise ValueError(f"not an integer: {s!r}")
            return int(d)
        return conv
    if t.startswith(("NUMERIC", "DECIMAL", "DECFLOAT")):
        m = re.search(r"\(\d+,(\d+)\)", t)
        q = decimal.Decimal(1).scaleb(-int(m.group(1))) if m else None

        def conv(v):
            if blank(v):
                return None
            d = decimal.Decimal(str(v).strip()) if not isinstance(v, decimal.Decimal) else v
            return d.quantize(q, rounding=decimal.ROUND_HALF_UP) if q is not None else d
        return conv
    if t.startswith(("FLOAT", "DOUBLE", "REAL")):
        return lambda v: None if blank(v) else float(v)
    if t.startswith("BOOLEAN"):
        def conv(v):
            if blank(v):
                return None
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            s = str(v).strip().lower()
            if s in _BOOL:
                return _BOOL[s]
            if s in ("1", "0"):
                return s == "1"
            raise ValueError(f"not a boolean: {v!r}")
        return conv
    if t.startswith("TIMESTAMP"):
        def conv(v):
            if blank(v):
                return None
            if isinstance(v, dt.datetime):
                return v
            if isinstance(v, dt.date):
                return dt.datetime(v.year, v.month, v.day)
            return _parse_ts(str(v))
        return conv
    if t.startswith("DATE"):
        def conv(v):
            if blank(v):
                return None
            if isinstance(v, dt.datetime):
                return v.date()
            if isinstance(v, dt.date):
                return v
            return _parse_date(str(v))
        return conv
    if t.startswith("TIME"):
        def conv(v):
            if blank(v):
                return None
            if isinstance(v, dt.datetime):
                return v.time()
            if isinstance(v, dt.time):
                return v
            return dt.time.fromisoformat(str(v).strip())
        return conv
    binary = "OCTETS" in t or t.startswith("BLOB SUB_TYPE BINARY") or t.startswith(("BINARY", "VARBINARY"))
    m = re.match(r"(VAR)?CHAR\((\d+)\)", t)
    maxlen = int(m.group(2)) if m else None

    def conv(v):
        if v is None or (empty_as_null and isinstance(v, str) and v == ""):
            return None
        if binary and isinstance(v, str) and v.startswith("0x"):
            return bytes.fromhex(v[2:])
        if isinstance(v, (dict, list)):
            s = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            s = "true" if v else "false"
        elif isinstance(v, (dt.date, dt.time, dt.datetime)):
            s = v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
        else:
            s = str(v)
        if maxlen is not None and len(s) > maxlen:
            raise ValueError(f"value longer than {maxlen} characters")
        return s
    return conv


# ------------------------------------------------------------------ import
def run_import(conn, log, *, path: Path, fmt: str, options: dict, table: str, create: bool,
               columns: list[dict], truncate: bool = False, on_error: str = "abort",
               result: dict | None = None, progress_every: int = 5000) -> dict:
    """Load the upload into ``table`` in one transaction. ``columns``: [{source, target, type}]."""
    from ..metadata import Catalog

    result = result if result is not None else {}
    result.update(inserted=0, failed=0, errors=[], table=table)
    mapping = [c for c in columns if c.get("target") and c.get("source") is not None]
    if not mapping:
        raise ValueError("Map at least one column")
    cur = conn.cursor()
    qt = quote_ident(table)
    if create:
        defs = ",\n    ".join(f"{quote_ident(c['target'])} {c.get('type') or 'VARCHAR(255)'}" for c in mapping)
        ddl = f"CREATE TABLE {qt} (\n    {defs}\n)"
        log(ddl)
        cur.execute(ddl)
        conn.commit()
        result["created"] = True
    cat = Catalog(conn)
    meta = {c["name"]: c for c in cat.columns(table)}
    if not meta:
        raise LookupError(f"Table {table!r} not found")
    for c in mapping:
        if c["target"] not in meta:
            raise ValueError(f"Column {c['target']!r} does not exist in {table}")
    if truncate and not create:
        cur.execute(f"DELETE FROM {qt}")
        log(f"Deleted {cur.rowcount} existing row(s)")
    empty_as_null = options.get("empty_as_null", True)
    convs = [converter(meta[c["target"]]["data_type"], empty_as_null) for c in mapping]
    reader = Reader(path, fmt, options)
    try:
        idx = {name: i for i, name in enumerate(reader.columns)}
        src_idx = []
        for c in mapping:
            s = c["source"]
            if isinstance(s, int):
                src_idx.append(s)
            elif s in idx:
                src_idx.append(idx[s])
            else:
                raise ValueError(f"Source column {s!r} not found in the file")
        sql = (f"INSERT INTO {qt} (" + ", ".join(quote_ident(c["target"]) for c in mapping) + ") VALUES ("
               + ", ".join("?" for _ in mapping) + ")")
        stmt = cur.prep(sql)
        line = 1 if (fmt != "json" and options.get("header", True)) else 0
        for row in reader.rows():
            line += 1
            try:
                values = [conv(row[i] if i < len(row) else None) for conv, i in zip(convs, src_idx, strict=True)]
                cur.execute(stmt, values)
                result["inserted"] += 1
            except Exception as exc:  # noqa: BLE001 - every row error is reported
                result["failed"] += 1
                msg = str(exc).strip().replace("\n", " ")
                if len(result["errors"]) < 200:
                    result["errors"].append({"row": line, "message": msg})
                if on_error == "abort":
                    conn.rollback()
                    raise ValueError(f"Row {line}: {msg} - nothing was imported") from exc
            if result["inserted"] and result["inserted"] % progress_every == 0:
                log(f"{result['inserted']} rows inserted")
        conn.commit()
    finally:
        reader.close()
    log(f"Imported {result['inserted']} row(s) into {table}" +
        (f", {result['failed']} row(s) failed" if result["failed"] else ""))
    for e in result["errors"][:20]:
        log(f"  row {e['row']}: {e['message']}")
    return result


# ------------------------------------------------------------------ export
def sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, decimal.Decimal)):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"x'{bytes(v).hex().upper()}'"
    if isinstance(v, dt.datetime):
        return f"'{v.isoformat(sep=' ')}'"
    if isinstance(v, (dt.date, dt.time)):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex().upper()
    return str(v)


def export_rows(cur, fmt: str, *, table_name: str = "EXPORT", delimiter: str = ",", header: bool = True,
                batch: int = 1000) -> Iterator[bytes]:
    """Stream the rows of an executed cursor in ``fmt`` (csv, json, sql). XLSX: see export_xlsx."""
    names = [d[0] for d in cur.description]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        if header:
            w.writerow(names)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for r in rows:
                w.writerow([_text(v) for v in r])
            yield buf.getvalue().encode()
            buf.seek(0)
            buf.truncate()
        if buf.getvalue():
            yield buf.getvalue().encode()
    elif fmt == "json":
        yield b"["
        first = True
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            parts = []
            for r in rows:
                parts.append(("\n  " if first else ",\n  ") + json.dumps(
                    {n: to_json(v) for n, v in zip(names, r, strict=True)}, ensure_ascii=False))
                first = False
            yield "".join(parts).encode()
        yield b"\n]\n"
    elif fmt == "sql":
        cols = ", ".join(quote_ident(n) for n in names)
        qt = quote_ident(table_name)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield "".join(f"INSERT INTO {qt} ({cols}) VALUES ({', '.join(sql_literal(v) for v in r)});\n"
                          for r in rows).encode()
    else:
        raise ValueError(f"Unknown export format {fmt!r}")


def export_xlsx(cur, sheet: str = "Export", batch: int = 1000) -> tempfile.SpooledTemporaryFile:
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=re.sub(r"[\[\]:*?/\\]", "_", sheet)[:31] or "Export")
    ws.append([d[0] for d in cur.description])
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        for r in rows:
            ws.append([_xlsx_value(v) for v in r])
    out = tempfile.SpooledTemporaryFile(max_size=32 << 20)
    wb.save(out)
    out.seek(0)
    return out


def _xlsx_value(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex().upper()
    if isinstance(v, decimal.Decimal):
        return float(v) if abs(v) < 10**15 else str(v)
    if isinstance(v, int) and abs(v) > 2**53:
        return str(v)
    if isinstance(v, dt.datetime) and v.tzinfo:
        return v.replace(tzinfo=None)
    if isinstance(v, dt.time) and v.tzinfo:
        return v.replace(tzinfo=None)
    return v

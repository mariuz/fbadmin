"""Small, guarded fixes for the pure-Python ``firebirdsql`` driver.

1. Binary parameters: the driver sends short ``bytes`` values as untyped text (``blr_text``),
   so the server interprets them in the connection charset. Firebird 6 validates that
   (``Malformed string`` for non-UTF-8 bytes). Send them as ``blr_text2`` with charset OCTETS
   instead; ``str`` parameters keep the old encoding.

3. Result descriptors: ``calc_blr`` describes CHAR/VARCHAR outputs without a charset. Firebird 6
   then mis-sizes or converts them (``string right truncation`` on ASCII system columns,
   ``Malformed string`` on OCTETS). Describe them like fbclient does: ``blr_text2`` /
   ``blr_varying2`` with the charset the server reported.

2. ``CHAR``/``VARCHAR`` columns with ``CHARACTER SET OCTETS`` (e.g. binary UUIDs) are decoded
   as text by the driver and raise ``UnicodeDecodeError``. Return ``bytes`` for them, and fall
   back to ``bytes`` for any value that is not valid in the connection charset.

Each patch only applies if the driver source still looks the way we expect.
"""

from __future__ import annotations

import inspect
import logging
import re
import textwrap

log = logging.getLogger("fbadmin.driver")
_applied = False

_TEXT_LINE = "blr += bytes([14, nbytes & 255, nbytes >> 8])"


def apply() -> None:
    global _applied
    if _applied:
        return
    _applied = True
    _patch_octets_fetch()
    _patch_calc_blr()
    try:
        from firebirdsql import wireprotocol

        fn = wireprotocol.WireProtocol.params_to_blr
        src = textwrap.dedent(inspect.getsource(fn))
        m = re.search(r"\n( +)if isinstance\(p, str\):\n", src)
        if m is None or _TEXT_LINE not in src:
            log.info("firebirdsql params_to_blr changed upstream; binary parameter patch skipped")
            return
        indent = m.group(1)
        # first occurrences: the str->bytes conversion and the short-bytes branch
        src = src.replace(m.group(0), f"\n{indent}_was_str = isinstance(p, str)\n{indent}if _was_str:\n", 1)
        src = src.replace(
            _TEXT_LINE,
            "blr += bytes([14, nbytes & 255, nbytes >> 8]) if _was_str else "
            "bytes([15, 1, 0, nbytes & 255, nbytes >> 8])  # blr_text2 + OCTETS",
            1,
        )
        ns: dict = {}
        exec(compile(src, "<fbadmin driver patch>", "exec"), wireprotocol.__dict__, ns)  # noqa: S102
        wireprotocol.WireProtocol.params_to_blr = ns["params_to_blr"]
    except Exception:  # noqa: BLE001 - never break startup because of a patch
        log.exception("could not patch firebirdsql params_to_blr")


def _patch_octets_fetch() -> None:
    try:
        from firebirdsql import xsqlvar
        from firebirdsql.consts import SQL_TYPE_TEXT, SQL_TYPE_VARYING

        original = xsqlvar.XSQLVAR.value

        def value(self, raw_value):
            if self.sqltype in (SQL_TYPE_TEXT, SQL_TYPE_VARYING):
                if self.sqlsubtype is not None and (self.sqlsubtype & 0xFF) == 1:  # OCTETS
                    return bytes(raw_value)
                try:
                    return original(self, raw_value)
                except UnicodeDecodeError:
                    return bytes(raw_value)
            return original(self, raw_value)

        xsqlvar.XSQLVAR.value = value
    except Exception:  # noqa: BLE001
        log.exception("could not patch firebirdsql XSQLVAR.value")


def _patch_calc_blr() -> None:
    try:
        from firebirdsql import fbcore, xsqlvar
        from firebirdsql.consts import SQL_TYPE_TEXT, SQL_TYPE_VARYING

        original = xsqlvar.calc_blr

        def calc_blr(xsqlda):
            # Like fbclient: describe text outputs as blr_text2 / blr_varying2 carrying the
            # charset the server reported for the column (sqlsubtype low byte).
            out = bytearray()
            for x in xsqlda:
                if x.sqltype in (SQL_TYPE_TEXT, SQL_TYPE_VARYING):
                    cs = (x.sqlsubtype or 0) & 0xFF
                    code = 15 if x.sqltype == SQL_TYPE_TEXT else 38
                    out += bytes([code, cs, 0, x.sqllen & 255, x.sqllen >> 8, 7, 0])
                else:
                    out += bytes(original([x]))[6:-2]  # strip message header and blr_end/eoc
            ln = len(xsqlda) * 2
            return bytes([5, 2, 4, 0, ln & 255, ln >> 8]) + bytes(out) + bytes([255, 76])

        xsqlvar.calc_blr = calc_blr
        fbcore.calc_blr = calc_blr
        try:
            from firebirdsql.aio import fbcore as aio_fbcore

            aio_fbcore.calc_blr = calc_blr
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        log.exception("could not patch firebirdsql calc_blr")

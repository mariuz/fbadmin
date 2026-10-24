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

4. Firebird 4+ types (see ``fbadmin/fb4types.py``): the driver decodes INT128 (and NUMERIC/DECIMAL
   with precision > 18, stored as INT128) wrongly when the low 64 bits have the top bit set, drops
   the time zone of offset zones (``+05:30`` comes back as UTC), converts TIME WITH TIME ZONE
   in region zones with today's date instead of Firebird's base date (2020-01-01), and cannot
   read the ``EXTENDED`` variants (``SET BIND OF TIME ZONE TO EXTENDED``). As parameters, integers
   beyond BIGINT and decimals with more than 18 digits are silently truncated to 64 bits, offset
   time zones raise ``KeyError`` and DECFLOAT-range decimals raise ``ValueError``. DECFLOAT values
   with all 16 / 34 digits and a leading 8 or 9 are decoded wrongly (operator precedence in
   ``decfloat.py``). Values are decoded by ``fb4types.decode``, time zone values are fetched in
   the EXTENDED format (with the server's offset), and such parameters are encoded by
   ``fb4types.encode_param`` (INT128, DECFLOAT(34), TIME/TIMESTAMP WITH TIME ZONE).

5. Service connections opened inside a cancellable job send the process name ``jobs.service_tag()``
   (``isc_spb_process_name``), so the attachments of their gbak/gfix actions can be found in
   ``MON$ATTACHMENTS`` and detached when the job is cancelled (see ``fbadmin/jobs.py``).

Each patch only applies if the driver source still looks the way we expect.
"""

from __future__ import annotations

import inspect
import logging
import re
import textwrap

log = logging.getLogger("fbadmin.driver")
_applied = False



def apply() -> None:
    global _applied
    if _applied:
        return
    _applied = True
    _patch_octets_fetch()
    _patch_fb4_types()
    _patch_calc_blr()
    _patch_service_process_name()
    try:
        from firebirdsql import wireprotocol

        from . import fb4types

        fn = wireprotocol.WireProtocol.params_to_blr
        src = textwrap.dedent(inspect.getsource(fn))
        m = re.search(r"\n( +)if isinstance\(p, str\):\n", src)
        if m is None:
            log.info("firebirdsql params_to_blr changed upstream; binary parameter patch skipped")
            return
        indent = m.group(1)
        # first occurrences: the str->bytes conversion and the short-bytes branch
        src = src.replace(m.group(0), f"\n{indent}_was_str = isinstance(p, str)\n{indent}if _was_str:\n", 1)
        hook = re.search(r"\n( +)t = type\(p\)\n\1if p is None:\n", src)
        if hook is None:
            log.info("firebirdsql params_to_blr changed upstream; INT128/time zone parameter patch skipped")
        else:
            h = hook.group(1)
            src = src[: hook.start()] + (
                f"\n{h}t = type(p)\n{h}_x = _special_param(self, p)\n{h}if _x is not None:\n"
                f"{h}    v, _b = _x\n{h}    blr += _b\n{h}elif p is None:\n"
            ) + src[hook.end():]
        block = re.search(
            r"\n( +)v = p\n\1nbytes = len\(v\)\n\1pad_length = \(\(4-nbytes\) & 3\)\n"
            r"\1v \+= bytes\(\[0\]\) \* pad_length\n\1blr \+= bytes\(\[14, nbytes & 255, nbytes >> 8\]\)\n",
            src,
        )
        if block is None:
            log.info("firebirdsql params_to_blr changed upstream; parameter patch skipped")
            return
        ind = block.group(1)
        replacement = (
            f"\n{ind}v, _blr = _param_value(self, p, _was_str)\n"
            f"{ind}blr += _blr\n"
        )
        src = src[: block.start()] + replacement + src[block.end():]
        ns: dict = {}
        wireprotocol._param_value = _param_value
        wireprotocol._special_param = fb4types.encode_param
        exec(compile(src, "<fbadmin driver patch>", "exec"), wireprotocol.__dict__, ns)  # noqa: S102
        wireprotocol.WireProtocol.params_to_blr = ns["params_to_blr"]
    except Exception:  # noqa: BLE001 - never break startup because of a patch
        log.exception("could not patch firebirdsql params_to_blr")


ISC_SPB_PROCESS_NAME = 112


def _tag_service_attach(buf: bytes, tag: str) -> bytes:
    """Insert ``isc_spb_process_name`` into an ``op_service_attach`` packet (unchanged if it is not one)."""
    try:
        if int.from_bytes(buf[:4], "big") != 82:  # op_service_attach
            return buf
        n = int.from_bytes(buf[8:12], "big")
        at = 12 + n + (-n & 3)
        ln = int.from_bytes(buf[at:at + 4], "big")
        spb = buf[at + 4:at + 4 + ln]
        if spb[:2] != bytes([2, 2]) or len(buf) != at + 4 + ln + (-ln & 3):
            return buf
        name = tag.encode()[:250]
        spb = spb[:2] + bytes([ISC_SPB_PROCESS_NAME, len(name)]) + name + spb[2:]
        return buf[:at] + len(spb).to_bytes(4, "big") + spb + bytes(-len(spb) & 3)
    except Exception:  # noqa: BLE001 - never break the attach
        return buf


class _TaggingSocket:
    def __init__(self, sock, tag: str):
        self._sock, self._tag = sock, tag

    def send(self, buf):
        return self._sock.send(_tag_service_attach(bytes(buf), self._tag))

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _patch_service_process_name() -> None:
    try:
        from firebirdsql import wireprotocol

        orig = wireprotocol.WireProtocol._op_service_attach
        if getattr(orig, "_fbadmin", False):
            return

        def _op_service_attach(self):
            from .jobs import service_tag

            tag = service_tag()
            if not tag or self.sock is None:
                return orig(self)
            sock = self.sock
            self.sock = _TaggingSocket(sock, tag)
            try:
                return orig(self)
            finally:
                self.sock = sock

        _op_service_attach._fbadmin = True  # type: ignore[attr-defined]
        wireprotocol.WireProtocol._op_service_attach = _op_service_attach
    except Exception:  # noqa: BLE001
        log.exception("could not patch firebirdsql service attach")


def _param_value(conn, p: bytes, was_str: bool) -> tuple[bytes, bytes]:
    """Value bytes and BLR descriptor for a short text/binary parameter.

    Text keeps the driver's original untyped ``blr_text`` (the server converts it from the
    connection charset). Bytes are sent as ``blr_text2`` with charset OCTETS, so binary data is
    never validated as text (``Malformed string`` on Firebird 6).
    """
    n = len(p)
    blr = bytes([14, n & 255, n >> 8]) if was_str else bytes([15, 1, 0, n & 255, n >> 8])
    return p + bytes((4 - n) & 3), blr


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


def _patch_fb4_types() -> None:
    try:
        from firebirdsql import xsqlvar

        from . import fb4types

        # Fetch time zone values in the EXTENDED format (with the server's offset); the EXTENDED
        # types themselves (SET BIND OF TIME ZONE TO EXTENDED) are unknown to the driver.
        xsqlvar.XSQLVAR.type_length.update(fb4types.WIRE_LENGTH)
        xsqlvar.XSQLVAR.type_display_length.update(fb4types.DISPLAY_LENGTH)
        xsqlvar.sqltype2blr.update(fb4types.OUTPUT_BLR)
        original = xsqlvar.XSQLVAR.value

        def value(self, raw_value):
            if self.sqltype in fb4types.FB4_TYPES:
                try:
                    return fb4types.decode(self.sqltype, self.sqlscale or 0, bytes(raw_value))
                except Exception:  # noqa: BLE001 - raising here would break the fetch stream
                    log.exception("could not decode a value of type %s", self.sqltype)
                    return bytes(raw_value)
            return original(self, raw_value)

        xsqlvar.XSQLVAR.value = value
    except Exception:  # noqa: BLE001
        log.exception("could not patch firebirdsql INT128 / time zone decoding")


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

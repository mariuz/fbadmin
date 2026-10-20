"""Wire decoding/encoding of the Firebird 4+ types the ``firebirdsql`` driver gets wrong.

Used by :mod:`fbadmin.driver_patches`. Wire formats (XDR, big-endian, every field padded to 4 bytes):

* ``INT128`` (sqltype 32752, also NUMERIC/DECIMAL with precision > 18): 16-byte two's complement.
* ``TIMESTAMP WITH TIME ZONE`` (32754): date (days since 1858-11-17), time (1/10000 s), zone id
  - all **UTC**, the zone only says how to show it. ``TIME WITH TIME ZONE`` (32756): time, zone id.
* The ``EXTENDED`` variants (32748 / 32750, after ``SET BIND OF TIME ZONE TO EXTENDED``) add the
  offset in minutes the server computed for the value.
* Zone ids: ``0..2878`` are fixed offsets (``id - 1439`` minutes); region ids count down from
  65535 (GMT). The ids are fixed in the Firebird sources (``src/common/TimeZones.h``, new zones
  are only appended) and the driver's ``tz_map`` matches ``RDB$TIME_ZONES`` of Firebird 4, 5 and 6.
* A TIME WITH TIME ZONE in a region zone is converted with the zone's offset on 2020-01-01
  (Firebird's ``TimeZoneUtil`` base date), not on the current date.
* ``DECFLOAT(16)`` / ``DECFLOAT(34)`` (32760 / 32762): IEEE 754 decimal64 / decimal128 with
  densely packed decimal coefficients.

fbadmin fetches time zone values in the EXTENDED format (see OUTPUT_BLR), so the local time and
offset are always the server's. Values become ``int`` / ``decimal.Decimal`` and time zone aware
``datetime.datetime`` / ``datetime.time``: region zones get a ``zoneinfo.ZoneInfo`` (an
:class:`FbZone` for Firebird names zoneinfo does not know, or with a fixed offset where Python's
time zone rules disagree with the server's or are missing), offset zones a ``datetime.timezone``.
"""

from __future__ import annotations

import datetime as dt
import decimal
import functools
import logging
import struct

log = logging.getLogger("fbadmin.driver")

SQL_TIMESTAMP_TZ_EX = 32748
SQL_TIME_TZ_EX = 32750
SQL_INT128 = 32752
SQL_TIMESTAMP_TZ = 32754
SQL_TIME_TZ = 32756
SQL_DEC16 = 32760
SQL_DEC34 = 32762
FB4_TYPES = frozenset({SQL_TIMESTAMP_TZ_EX, SQL_TIME_TZ_EX, SQL_INT128, SQL_TIMESTAMP_TZ, SQL_TIME_TZ,
                       SQL_DEC16, SQL_DEC34})

# XSQLVAR tables: io length, display length and output BLR. Time zone values are always fetched
# in the EXTENDED format (blr_ex_time_tz = 30, blr_ex_timestamp_tz = 31; the client chooses the
# output message format and the server converts, as for ``SET BIND``): the server then sends the
# offset it computed with its own time zone database, so values show exactly what Firebird shows,
# even where Python's tz database differs or is missing (Windows without ``tzdata``).
WIRE_LENGTH = {SQL_TIMESTAMP_TZ_EX: 16, SQL_TIME_TZ_EX: 12, SQL_TIMESTAMP_TZ: 16, SQL_TIME_TZ: 12}
DISPLAY_LENGTH = {SQL_TIMESTAMP_TZ_EX: 28, SQL_TIME_TZ_EX: 17}
OUTPUT_BLR = {SQL_TIMESTAMP_TZ_EX: [31], SQL_TIME_TZ_EX: [30], SQL_TIMESTAMP_TZ: [31], SQL_TIME_TZ: [30]}

ONE_DAY = 1439  # minutes; offset zone id = ONE_DAY + offset minutes
EPOCH = dt.datetime(1858, 11, 17)  # Firebird day 0
TIME_TZ_BASE_DATE = dt.date(2020, 1, 1)
_UTC = dt.timezone.utc
_DAY_US = 86400 * 1_000_000

# Firebird (ICU) zone names that are not in the IANA database ``zoneinfo`` uses: the old
# Java-style ids and removed links. Resolved like ICU does; the value keeps the Firebird name.
ZONE_ALIASES = {
    "ACT": "Australia/Darwin", "AET": "Australia/Sydney", "AGT": "America/Argentina/Buenos_Aires",
    "ART": "Africa/Cairo", "AST": "America/Anchorage", "BET": "America/Sao_Paulo", "BST": "Asia/Dhaka",
    "CAT": "Africa/Harare", "CNT": "America/St_Johns", "CST": "America/Chicago", "CTT": "Asia/Shanghai",
    "EAT": "Africa/Addis_Ababa", "ECT": "Europe/Paris", "IET": "America/Indiana/Indianapolis",
    "IST": "Asia/Kolkata", "JST": "Asia/Tokyo", "MIT": "Pacific/Apia", "NET": "Asia/Yerevan",
    "NST": "Pacific/Auckland", "PLT": "Asia/Karachi", "PNT": "America/Phoenix", "PRT": "America/Puerto_Rico",
    "PST": "America/Los_Angeles", "SST": "Pacific/Guadalcanal", "VST": "Asia/Ho_Chi_Minh",
    "Canada/East-Saskatchewan": "America/Regina", "US/Pacific-New": "America/Los_Angeles",
    "SystemV/AST4": "Etc/GMT+4", "SystemV/AST4ADT": "America/Halifax", "SystemV/CST6": "Etc/GMT+6",
    "SystemV/CST6CDT": "America/Chicago", "SystemV/EST5": "Etc/GMT+5", "SystemV/EST5EDT": "America/New_York",
    "SystemV/HST10": "Etc/GMT+10", "SystemV/MST7": "Etc/GMT+7", "SystemV/MST7MDT": "America/Denver",
    "SystemV/PST8": "Etc/GMT+8", "SystemV/PST8PDT": "America/Los_Angeles", "SystemV/YST9": "Etc/GMT+9",
    "SystemV/YST9YDT": "America/Anchorage",
}


class FbZone(dt.tzinfo):
    """A region zone known to Firebird under a name ``zoneinfo`` does not have (see ZONE_ALIASES)."""

    def __init__(self, key: str, inner: dt.tzinfo):
        self.key = key
        self._inner = inner

    def utcoffset(self, d):
        return self._inner.utcoffset(d)

    def dst(self, d):
        return self._inner.dst(d)

    def tzname(self, d):
        return self.key

    def fromutc(self, d):
        return self._inner.fromutc(d.replace(tzinfo=self._inner)).replace(tzinfo=self)

    def __repr__(self):
        return f"FbZone({self.key!r})"

    def __str__(self):
        return self.key


def _tz_map():
    from firebirdsql.tz_map import timezone_id_by_name, timezone_name_by_id

    return timezone_name_by_id, timezone_id_by_name


def _load_zone(name: str) -> dt.tzinfo | None:
    import zoneinfo

    for candidate in (name, ZONE_ALIASES.get(name)):
        if not candidate:
            continue
        try:
            z = zoneinfo.ZoneInfo(candidate)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            continue
        return z if candidate == name else FbZone(name, z)
    return None


@functools.cache
def zone_by_id(zone_id: int) -> dt.tzinfo | None:
    """tzinfo for a Firebird zone id; None if no time zone rules are available for it (values
    then get a fixed offset from the server, see _region_tz)."""
    if zone_id <= 2 * ONE_DAY:
        return dt.timezone(dt.timedelta(minutes=zone_id - ONE_DAY))
    name = _tz_map()[0].get(zone_id)
    z = _load_zone(name) if name else None
    if z is None:
        log.debug("no Python time zone rules for Firebird zone id %s (%s)", zone_id, name)
    return z


def zone_name(tz: dt.tzinfo | None) -> str | None:
    """The region name of a tzinfo (ZoneInfo, FbZone and similar), None for fixed offsets."""
    if tz is None or isinstance(tz, dt.timezone):
        return None
    # zoneinfo / pytz / firebird-driver (the native driver, FBADMIN_DRIVER=native)
    key = getattr(tz, "key", None) or getattr(tz, "zone", None) or getattr(tz, "_timezone_", None)
    return key if isinstance(key, str) else None


# ------------------------------------------------------------------ decoding
def decode(sqltype: int, scale: int, raw: bytes):
    """Python value of a raw INT128 / TIME [STAMP] WITH TIME ZONE (plain or EXTENDED) value."""
    if sqltype == SQL_INT128:
        n = int.from_bytes(raw[:16], "big", signed=True)
        return decimal.Decimal(f"{n}E{scale}") if scale else n
    if sqltype in (SQL_DEC16, SQL_DEC34):
        return decode_decfloat(raw[:8] if sqltype == SQL_DEC16 else raw[:16])
    if sqltype in (SQL_TIMESTAMP_TZ, SQL_TIMESTAMP_TZ_EX):
        days = int.from_bytes(raw[0:4], "big", signed=True)
        ticks = int.from_bytes(raw[4:8], "big")
        zone = int.from_bytes(raw[10:12], "big")  # USHORT, sent as a 4-byte XDR short
        ext = int.from_bytes(raw[14:16], "big", signed=True) if len(raw) >= 16 else None
        return _timestamp(days, ticks, zone, ext)
    ticks = int.from_bytes(raw[0:4], "big")
    zone = int.from_bytes(raw[6:8], "big")
    ext = int.from_bytes(raw[10:12], "big", signed=True) if len(raw) >= 12 else None
    return _time(ticks, zone, ext)


_DPD: dict[int, int] = {}


def decode_decfloat(raw: bytes) -> decimal.Decimal:
    """IEEE 754 decimal64 / decimal128 (DPD, big-endian): DECFLOAT(16) / DECFLOAT(34)."""
    nbits = len(raw) * 8
    ecbits, bias = (8, 398) if nbits == 64 else (12, 6176)
    n = int.from_bytes(raw, "big")
    sign = n >> (nbits - 1)
    g = (n >> (nbits - 6)) & 0b11111
    if g == 0b11111:
        nan = "sNaN" if (n >> (nbits - 7)) & 1 else "NaN"
        return decimal.Decimal(("-" if sign else "") + nan)
    if g == 0b11110:
        return decimal.Decimal("-Infinity" if sign else "Infinity")
    if g >> 3 == 0b11:  # leading digit 8 or 9
        exp_msb, lead = (g >> 1) & 0b11, 8 + (g & 1)
    else:
        exp_msb, lead = g >> 3, g & 0b111
    cbits = nbits - 6 - ecbits
    exp = (exp_msb << ecbits) | ((n >> cbits) & ((1 << ecbits) - 1))
    if not _DPD:
        _DPD.update({_declet(f"{i:03d}"): i for i in range(1000)})
    coef = lead
    for shift in range(cbits - 10, -1, -10):
        d = (n >> shift) & 0x3FF
        coef = coef * 1000 + (_DPD[d] if d in _DPD else _dpd_noncanonical(d))
    return decimal.Decimal((sign, tuple(int(c) for c in str(coef)), exp - bias))


def _dpd_noncanonical(d: int) -> int:
    from firebirdsql.decfloat import dpd_to_int

    return dpd_to_int(d)


def _naive(total_us: int) -> dt.datetime:
    return EPOCH + dt.timedelta(microseconds=total_us)


def _minutes(off: dt.timedelta) -> dt.timedelta:
    """Firebird offsets are whole minutes (truncated, as Firebird does): old local mean times
    such as -04:56:02 become -04:56."""
    s = off // dt.timedelta(seconds=1)
    return dt.timedelta(minutes=abs(s) // 60 * (-1 if s < 0 else 1))


def _region_tz(zone: int, tz: dt.tzinfo | None, local: dt.datetime, off: dt.timedelta) -> dt.datetime:
    """``local`` with the region zone ``tz`` if Python's rules give the server's offset ``off``
    (trying both folds), else with a fixed-offset zone of the same name, so the value keeps
    the server's instant and zone even where the time zone databases differ."""
    if tz is not None:
        for fold in (0, 1):
            v = local.replace(tzinfo=tz, fold=fold)
            u = v.utcoffset()
            if u is not None and _minutes(u) == off:
                return v
    name = _tz_map()[0].get(zone)
    return local.replace(tzinfo=FbZone(name, dt.timezone(off)) if name else dt.timezone(off))


def _timestamp(days: int, ticks: int, zone: int, ext: int | None) -> dt.datetime | str:
    utc_us = days * _DAY_US + ticks * 100
    tz = zone_by_id(zone)
    if ext is not None:  # the server sent the offset (EXTENDED format): exactly what it shows
        off = dt.timedelta(minutes=ext)
    elif tz is None:
        tz, off = _UTC, dt.timedelta(0)
    elif isinstance(tz, dt.timezone):
        off = tz.utcoffset(None)
    else:
        try:
            off = _minutes(_naive(utc_us).replace(tzinfo=_UTC).astimezone(tz).utcoffset())
        except (OverflowError, ValueError):  # within a day of 0001-01-01 / 9999-12-31 in UTC
            off = _minutes(tz.utcoffset(dt.datetime(2, 1, 1) if days < 0 else dt.datetime(9998, 12, 31)))
    local_us = utc_us + off // dt.timedelta(microseconds=1)
    try:
        local = _naive(local_us)
    except OverflowError:
        # Firebird allows e.g. 0001-01-01 00:00 UTC AT TIME ZONE '-03:00' (local year 0), Python
        # does not: return Firebird's text instead of failing the whole fetch.
        return _text_out_of_range(local_us, zone, off)
    if isinstance(tz, dt.timezone):
        return local.replace(tzinfo=tz)
    return _region_tz(zone, tz, local, off)


def _text_out_of_range(local_us: int, zone: int, off: dt.timedelta) -> str:
    days, us = divmod(local_us, _DAY_US)
    # proleptic Gregorian date from Firebird's day number (as isc_decode_date; works for year 0)
    nday = 4 * (days + 678882) - 1
    century = nday // 146097
    day = (nday - 146097 * century) // 4
    nday = (4 * day + 3) // 1461
    day = (4 * day + 3 - 1461 * nday + 4) // 4
    month = (5 * day - 3) // 153
    day = (5 * day - 3 - 153 * month + 5) // 5
    year = 100 * century + nday
    month, year = (month + 3, year) if month < 10 else (month - 9, year + 1)
    name = _tz_map()[0].get(zone) if zone > 2 * ONE_DAY else None
    if name is None:
        m = off // dt.timedelta(minutes=1)
        name = f"{'-' if m < 0 else '+'}{abs(m) // 60:02d}:{abs(m) % 60:02d}"
    s, frac = divmod(us // 100, 10000)
    return f"{year:04d}-{month:02d}-{day:02d} {s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}.{frac:04d} {name}"


def _ticks_time(ticks: int, tz: dt.tzinfo | None) -> dt.time:
    ticks %= 864_000_000
    s, frac = divmod(ticks, 10000)
    return dt.time(s // 3600, s // 60 % 60, s % 60, frac * 100, tzinfo=tz)


def _time(ticks: int, zone: int, ext: int | None) -> dt.time:
    tz = zone_by_id(zone)
    if isinstance(tz, dt.timezone):
        return _ticks_time(ticks + tz.utcoffset(None) // dt.timedelta(minutes=1) * 600_000, tz)
    base = dt.datetime.combine(TIME_TZ_BASE_DATE, dt.time(), tzinfo=_UTC) + dt.timedelta(microseconds=ticks * 100)
    if ext is not None:
        off = dt.timedelta(minutes=ext)
    elif tz is None:
        return _ticks_time(ticks, _UTC)
    else:  # region: Firebird uses the zone's offset on its base date 2020-01-01
        off = _minutes(base.astimezone(tz).utcoffset())
    return _region_tz(zone, tz, (base + off).replace(tzinfo=None), off).timetz()


# ------------------------------------------------------------------ encoding
def _zone_id(tz: dt.tzinfo, offset: dt.timedelta | None) -> int:
    name = zone_name(tz)
    ids = _tz_map()[1]
    if name is not None and name in ids:
        return ids[name]
    if name is None and not isinstance(tz, dt.timezone) and str(tz) in ids:
        return ids[str(tz)]
    if offset is None:
        raise ValueError(f"Unknown Firebird time zone {name or tz!r}")
    minutes, rest = divmod(offset, dt.timedelta(minutes=1))
    if rest or abs(minutes) > ONE_DAY:
        raise ValueError(f"Firebird time zone offsets are whole minutes up to 23:59, not {offset}")
    return ONE_DAY + minutes


def _declet(d: str) -> int:
    """Three decimal digits as a 10-bit densely packed decimal (IEEE 754-2008)."""
    a, b, c = (int(x) for x in d)
    big = (a > 7, b > 7, c > 7)
    a0, b0, c0 = a & 1, b & 1, c & 1
    if big == (False, False, False):
        return (a << 7) | (b << 4) | c
    if big == (False, False, True):
        return (a << 7) | (b << 4) | 0b1000 | c0
    if big == (False, True, False):
        return (a << 7) | ((c >> 1) << 5) | (b0 << 4) | 0b1010 | c0
    if big == (True, False, False):
        return ((c >> 1) << 8) | (a0 << 7) | (b << 4) | 0b1100 | c0
    if big == (False, True, True):
        return (a << 7) | (0b10 << 5) | (b0 << 4) | 0b1110 | c0
    if big == (True, False, True):
        return ((b >> 1) << 8) | (a0 << 7) | (0b01 << 5) | (b0 << 4) | 0b1110 | c0
    if big == (True, True, False):
        return ((c >> 1) << 8) | (a0 << 7) | (b0 << 4) | 0b1110 | c0
    return (a0 << 7) | (0b11 << 5) | (b0 << 4) | 0b1110 | c0


def decimal128(p: decimal.Decimal) -> bytes | None:
    """IEEE 754 decimal128 (DPD, big-endian) of ``p`` as Firebird's DECFLOAT(34); None if it
    does not fit (more than 34 digits, exponent out of range)."""
    sign, digits, exp = p.as_tuple()
    top = 1 << 127 if sign else 0
    if p.is_nan():
        return (top | (0b11111 << 122) | ((1 << 121) if p.is_snan() else 0)).to_bytes(16, "big")
    if p.is_infinite():
        return (top | (0b11110 << 122)).to_bytes(16, "big")
    coef = "".join(map(str, digits)).lstrip("0") or "0"
    while exp > 6111 and len(coef) < 34 and coef != "0":  # 1E+6144 -> 10E+6143
        coef, exp = coef + "0", exp - 1
    exp = min(max(exp, -6176), 6111) if coef == "0" else exp
    if len(coef) > 34 or not -6176 <= exp <= 6111:
        return None
    coef = coef.zfill(34)
    biased = exp + 6176
    lead = int(coef[0])
    if lead < 8:
        comb = ((biased >> 12) << 15) | (lead << 12) | (biased & 0xFFF)
    else:
        comb = (0b11 << 15) | ((biased >> 12) << 13) | ((lead & 1) << 12) | (biased & 0xFFF)
    n = top | (comb << 110)
    for i in range(11):
        n |= _declet(coef[1 + 3 * i: 4 + 3 * i]) << (100 - 10 * i)
    return n.to_bytes(16, "big")


def _int128(n: int) -> bytes:
    return n.to_bytes(16, "big", signed=True)


def _text(conn, s: str) -> tuple[bytes, bytes]:
    b = conn.str_to_bytes(s)
    n = len(b)
    return b + bytes((4 - n) & 3), bytes([14, n & 255, n >> 8])


def _int128_ok(conn) -> bool:
    return getattr(conn, "accept_version", 0) >= 16  # Firebird 4+ (protocol 16) knows blr_int128


def encode_param(conn, p):
    """``(value bytes, blr)`` for parameters the driver encodes wrongly; None = driver default."""
    t = type(p)
    if t is int:
        if -(2**63) <= p < 2**63:
            return None
        if _int128_ok(conn) and -(2**127) <= p < 2**127:
            return _int128(p), bytes([26, 0])
        return _text(conn, str(p))  # the server converts (and range-checks) the text
    if t is float:
        if p == p and p not in (float("inf"), float("-inf")):
            _, digits, exp = decimal.Decimal(str(p)).as_tuple()
            if -128 <= exp <= 127 and len(digits) <= 18:
                return None
        elif p == float("inf"):
            return None  # the driver sends +inf as a FLOAT
        return struct.pack(">d", p), bytes([27])  # blr_double
    if t is decimal.Decimal:
        fb4 = _int128_ok(conn)
        if p.is_finite():
            sign, digits, exp = p.as_tuple()
            coef = int("".join(map(str, digits)) or "0") * (-1 if sign else 1)
            if not (sign and not coef):  # DECFLOAT has a negative zero, integers do not
                if -128 <= exp <= 127 and -(2**63) <= coef < 2**63:
                    return None
                if -128 <= exp <= 127 and fb4 and -(2**127) <= coef < 2**127:
                    return _int128(coef), bytes([26, exp & 255])  # blr_int128 with scale
        # NaN, Infinity, -0, DECFLOAT exponents: a DECFLOAT(34) value (blr_dec128)
        raw = decimal128(p) if fb4 else None
        return (raw, bytes([25])) if raw is not None else _text(conn, str(p))
    if t is dt.datetime and p.tzinfo is not None:
        off = p.utcoffset()
        if off is None:
            return None
        if not isinstance(p.tzinfo, dt.timezone):
            off = _minutes(off)
        local_us = (p.replace(tzinfo=None) - EPOCH) // dt.timedelta(microseconds=1)
        days, us = divmod(local_us - off // dt.timedelta(microseconds=1), _DAY_US)
        zone = _zone_id(p.tzinfo, off)
        return (days.to_bytes(4, "big", signed=True) + (us // 100).to_bytes(4, "big")
                + zone.to_bytes(4, "big")), bytes([29])
    if t is dt.time and p.tzinfo is not None:
        tz = p.tzinfo
        if isinstance(tz, dt.timezone):
            off = tz.utcoffset(None)
        else:  # region zone: Firebird uses its offset on 2020-01-01
            off = dt.datetime.combine(TIME_TZ_BASE_DATE, p).utcoffset()
            off = None if off is None else _minutes(off)
        if off is None:
            return None
        local_us = ((p.hour * 60 + p.minute) * 60 + p.second) * 1_000_000 + p.microsecond
        us = (local_us - off // dt.timedelta(microseconds=1)) % _DAY_US
        zone = _zone_id(tz, off)
        return (us // 100).to_bytes(4, "big") + zone.to_bytes(4, "big"), bytes([28])
    return None

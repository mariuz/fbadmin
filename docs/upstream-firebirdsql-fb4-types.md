# firebirdsql: Firebird 4 data types decoded/encoded wrongly (draft upstream report)

Draft bug report for [nakagami/pyfirebirdsql](https://github.com/nakagami/pyfirebirdsql), found
with firebirdsql 1.4.7 against Firebird 4.0.7, 5.0.4 and a 6.0 snapshot. fbadmin works around
all of it in `backend/fbadmin/driver_patches.py` / `backend/fbadmin/fb4types.py`. Not filed yet.

Status with firebirdsql 1.5.0 (checked 2026-10-25 against Firebird 5.0.4, without fbadmin's patches):
#1 (INT128 decoding) is fixed; #2 (offset time zones), #4 (DECFLOAT) and #5 (INT128 parameters)
still return the wrong values shown below.

## Summary

| # | Where | Problem |
|---|-------|---------|
| 1 | `utils.bytes_to_bint` (16 bytes) | INT128 is read as `(hi << 64) \| lo` with **signed** `lo`: wrong whenever bit 63 of the low half is set (`170141183460469231731687303715884105727` → `-1`, `DECIMAL(38,0) 12345678901234567890123456789012345678` → `-4302749291975740594`). Fix: `int.from_bytes(b, "big", signed=True)`. |
| 2 | `XSQLVAR.value`, `SQL_TYPE_TIMESTAMP_TZ` / `SQL_TYPE_TIME_TZ` | The zone id is a USHORT sent as a 4-byte XDR short (`raw[8:12]`, low 16 bits). The driver reads `raw[8:10]` as "tz" and `raw[10:12]` as "offset". For region zones the high half is `ffff` (GMT), so it happens to work; for **offset zones** the high half is `0000` and the value comes back in UTC: `TIMESTAMP '2024-03-10 01:00 +05:30'` → `datetime(2024, 3, 9, 19, 30, tzinfo=utc)`. Offset zone ids are `1439 + offset minutes`. |
| 3 | `XSQLVAR.value`, `SQL_TYPE_TIME_TZ` | Region zones are converted with `date.today()`; Firebird uses the offset on 2020-01-01. `TIME '10:11:12 America/New_York'` comes back as 11:11:12 in summer. |
| 4 | `decfloat.py` | `significand_prefix = 8 + (combination_field >> 12) & 0b1` is `(8 + …) & 1` (operator precedence), also in `decimal64_to_decimal`. DECFLOAT values with all 16/34 digits and a leading 8 or 9 decode wrongly: `CAST('9999999999999999' AS DECFLOAT(16))` → `1999999999999999`. Fix: `8 + ((combination_field >> 12) & 1)`. |
| 5 | `params_to_blr`, `int` | Integers outside BIGINT are packed into 8 bytes **silently truncated**: `WHERE i = 2**127 - 1` matches `-1`; `INSERT` of `-(2**64) - 1` stores `-1`. Send `blr_int128` (26) with 16 bytes on protocol ≥ 16. |
| 6 | `params_to_blr`, `Decimal` | Same truncation for decimals with more than 18 digits (`Decimal('-1234567890123456789012345678901234.5678')` is stored as `430274929197574.0594`); exponents outside -128..127 raise `ValueError: bytes must be in range(0, 256)`; `NaN`/`Infinity` fail. Use `blr_int128` with scale, or `blr_dec128` (25). |
| 7 | `convert_time_tz` / `convert_timestamp_tz` | Fixed-offset `tzinfo` (`datetime.timezone(timedelta(hours=5, minutes=30))`) raises `KeyError: 'UTC+05:30'`; should send zone id `1439 + minutes`. `convert_time_tz` also uses `date.today()` instead of 2020-01-01. |
| 8 | `XSQLVAR.type_length`, `sqltype2blr` | After `SET BIND OF TIME ZONE TO EXTENDED` (sqltypes 32748/32750, `blr_ex_timestamp_tz` 31 / `blr_ex_time_tz` 30) every fetch fails with `KeyError: 32750`. |

A further, smaller point: `firebirdsql.tz_utils` returns `zoneinfo.ZoneInfo(name)`, which raises
for Firebird (ICU) names that are not in the IANA database (`ACT`, `AET`, `SystemV/EST5EDT`, …;
40 of the 638 ids even with the `tzdata` package) and for every name on Windows without `tzdata`.
Fetching time zone values as `blr_ex_*` (the client chooses the output format, the server adds the
offset it computed) avoids depending on the client's time zone database for the local time.

## Minimal reproduction

```python
import datetime as dt, decimal, firebirdsql

c = firebirdsql.create_database(host="localhost", user="SYSDBA", password="masterkey",
                                database="/tmp/repro.fdb")
cur = c.cursor()
cur.execute("select cast(170141183460469231731687303715884105727 as int128), "
            "cast(12345678901234567890123456789012345678 as decimal(38,0)), "
            "cast('9999999999999999' as decfloat(16)), "
            "timestamp '2024-03-10 01:00 +05:30', time '10:11:12 America/New_York' "
            "from rdb$database")
print(cur.fetchone())
# (-1, -4302749291975740594, Decimal('1999999999999999'),
#  datetime.datetime(2024, 3, 9, 19, 30, tzinfo=datetime.timezone.utc),
#  datetime.time(11, 11, 12, tzinfo=zoneinfo.ZoneInfo(key='America/New_York')))   # in summer

cur.execute("select cast(? as int128) from rdb$database", [2**100])
print(cur.fetchone())                                # (0,) - silently truncated
cur.execute("select cast(? as timestamp with time zone) from rdb$database",
            [dt.datetime(2024, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))])
# KeyError: 'UTC+05:30'
cur.execute("select cast(? as decfloat(34)) from rdb$database", [decimal.Decimal("1E-6000")])
# ValueError: bytes must be in range(0, 256)
cur.execute("set bind of time zone to extended")
cur.execute("select current_timestamp from rdb$database")
# KeyError: 32748
c.drop_database()
```

Expected (what `CAST(... AS VARCHAR(100))` returns): `170141183460469231731687303715884105727`,
`12345678901234567890123456789012345678`, `9999999999999999`,
`2024-03-10 01:00:00.0000 +05:30`, `10:11:12.0000 America/New_York`.

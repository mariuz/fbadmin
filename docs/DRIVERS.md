# Database drivers: native and firebirdsql

fbadmin talks to Firebird servers through one of two Python drivers. By default it picks one
itself (**Automatic**): the faster native driver when it can be used with a Firebird 4 or newer
client library, otherwise the pure-Python one. You can also choose one for the whole fbadmin
instance.

| | **native** (used by *Automatic* when possible) | **firebirdsql** (pure Python) |
|---|---|---|
| What it is | [`firebird-driver`](https://pypi.org/project/firebird-driver/), the official Python driver, a ctypes binding over the Firebird client library (`libfbclient.so` / `fbclient.dll` / `libfbclient.dylib`) | [`firebirdsql`](https://pypi.org/project/firebirdsql/), a pure-Python implementation of the Firebird wire protocol, plus fbadmin's fixes for Firebird 4+ types (`driver_patches.py`, `fb4types.py`) |
| Install | `firebird-driver` is a dependency of fbadmin (Python 3.11+). The client library is not on PyPI: it comes with the desktop app and the Docker image, or from a Firebird installation / your distribution (see [Which client library](#which-client-library)) | Nothing: it is a dependency of fbadmin |
| Runs on | Platforms with a Firebird client build (Linux x64/arm64, Windows, macOS) | Anything that runs Python |
| Strengths | Much faster for anything that moves data (see [Benchmarks](#benchmarks)), above all with wire encryption, which `firebirdsql` does in Python; the client Firebird itself ships, so new protocol features arrive with the library | No native code; same behaviour everywhere; no library to deploy |
| Weaknesses | One more native dependency; a process can load only one libfbclient; a Firebird 3 client cannot decode Firebird 4+ types | Slow at bulk data with wire encryption on (the Firebird default) |

**Local (embedded) databases** of the desktop app always use the native driver with the
bundled Firebird 5 engine, whatever is selected here (see [EMBEDDED.md](EMBEDDED.md)). The
setting on this page is about connections to Firebird **servers**.

## Automatic selection (the default)

With the setting **Automatic** (`auto`, the default when nothing is saved and `FBADMIN_DRIVER`
is not set), fbadmin uses the **native** driver when all of these hold, and **firebirdsql**
otherwise:

1. the `firebird-driver` package can be imported (it is a dependency on Python 3.11+; on
   Python 3.10 it is not installed: "firebird-driver is not installed");
2. a client library loads ("No Firebird client library found ..." or the load error);
3. it is a **Firebird 4 or newer** client. A Firebird 3 client would get INT128, DECFLOAT and
   the time zone types only as legacy types, which the pure driver reads exactly ("The client
   library is Firebird 3: ...");
4. that client can load **ICU**, which it needs for time zone names. Without ICU a Firebird
   client reads `TIMESTAMP '... Europe/Prague'` without its zone and cannot send one at all
   ("... cannot load ICU ..."). Linux distributions and Firebird's own packages install ICU;
   minimal containers may not.

The client library is looked for in this order: the embedded engine's file set when present
(the desktop app, or `FBADMIN_EMBEDDED_DIR`; one process can load only one libfbclient, and
local databases need that one), then the configured path (`FBADMIN_FBCLIENT` or the setting
in the panel; if it does not exist, Automatic uses firebirdsql and says so), then the system
library search path (firebird-driver's default lookup: `libfbclient.so` via `ldconfig`,
`fbclient.dll` on the `PATH`, the Firebird framework on macOS).

The decision is made once, when it is first needed, and again when the setting changes; the
log says which driver is in use and why at startup (INFO, logger `fbadmin.drivers`):

```text
INFO    fbadmin.drivers: Database driver: native (setting auto, default). Firebird 5.0 (LI-V6.3.4.1812) client library: libfbclient.so.2
```

**Safety net at run time.** If a native connection attempt fails for a reason that is not a
Firebird error, Automatic makes that connection with firebirdsql instead, logs a warning
once, and the panel shows "Native driver failed, using firebirdsql: ...". Such failures are:
the client library or `firebird-driver` cannot be loaded, the library raises an `OSError` or a
ctypes error, or it reports a problem with its own installation (a plugin, provider, ICU or
libtomcrypt it cannot load). When the library itself does not load, Automatic stops trying it
until the setting changes. **Firebird errors are never a reason to fall back**: a wrong
password, an unknown database, a refused connection or a server-side error are reported as
they are, whichever driver is in use. An explicitly selected driver never falls back either.

## Choosing and configuring

**In the UI:** open the user menu (in desktop mode the *Security* button in the top bar) →
**Database driver…**. In server mode only administrators see it. The panel offers:

- **Automatic (recommended): native when available, otherwise pure Python**;
- **Native (firebird-driver)**: always the native driver. It is disabled, and the API refuses
  it, when the native driver cannot be used: the package is missing, the library is not found
  or does not load, or it is older than Firebird 3;
- **Pure Python (firebirdsql)**: always the pure-Python driver.

It shows the selected setting and where it comes from (environment, saved setting or
default), **the driver in use and the reason** (for Automatic: which rule decided), a
run-time fallback if one happened, whether `firebird-driver` is installed and which version,
which libfbclient is loaded (or would be), its version, whether it can load ICU, and the
client library path (empty: the embedded engine's library when present, else the system
search path).

The choice is saved in `driver.json` in the data directory (`{"driver": "native", "fbclient":
"/opt/firebird/lib/libfbclient.so"}`). The API is `GET /api/driver` and `PUT /api/driver`
(`{"driver": "auto" | "native" | "firebirdsql", "fbclient": "<path>"}`), for administrators
only in server mode. `GET` also returns `effective` (`native` or `firebirdsql`), `reason` and
`fallback`.

**Environment variables win** over the saved setting, and the panel then shows "set by
environment" and keeps those fields read-only:

```bash
FBADMIN_DRIVER=auto                                    # or native, firebirdsql
FBADMIN_FBCLIENT=/opt/firebird/lib/libfbclient.so      # the client library
```

**What happens when you switch:**

- When the driver in use changes, the pooled metadata connections (object explorer, data
  grid, dashboards) are closed, so new ones use it. The panel says how many were closed.
- **Open Query Tool tabs keep their connection, and therefore their driver, until they are
  closed.** Closing them would throw away uncommitted transactions; a session that keeps
  running on the old driver is harmless. New Query Tool tabs use the new driver.
- The client library is loaded once per process. If a different library is already loaded,
  the panel says so; the new path takes effect after a restart.
- With the embedded engine's file set present (the desktop app, or `FBADMIN_EMBEDDED_DIR`),
  its Firebird 5 client library serves remote servers too.

### Which client library

Use a **Firebird 4 or 5 client** library; it works with Firebird 3, 4, 5 and 6 servers, and
Automatic uses the native driver with it. Where to get one:

- **Desktop app and Docker image:** included (Firebird 5.0.4), nothing to install.
- **A Firebird 4 or 5 server installation** on the same machine (the official installers and
  tarballs install the client library too).
- **Linux distributions:** the `libfbclient2` package. It is a **Firebird 4** client in Debian
  13 "trixie" (4.0.5) and newer, and in Ubuntu 25.04 (4.0.5), 25.10 and 26.04 (4.0.6). Debian
  12 "bookworm" and Ubuntu 24.04 LTS and older ship a **Firebird 3** client (3.0.11), with
  which Automatic keeps the pure-Python driver.
  A Firebird 4 client works with Firebird 5 servers, but it cannot send service options that are
  new in Firebird 5, such as **parallel workers** for backup, restore and sweep: fbadmin then
  offers only the options both the client library and the server understand, and says why. Use
  a Firebird 5 client (or the pure-Python driver) to get those options.
- **Elsewhere:** extract the client from the official release archive, e.g.
  `python packaging/embedded/fetch-embedded.py --client-only --dest /opt/firebird` (Linux;
  needs `libtommath1` and ICU from the system) and point `FBADMIN_FBCLIENT` at
  `/opt/firebird/lib/libfbclient.so.2`.

With **no** client library, or a Firebird 3 one, fbadmin works exactly as before with the
pure-Python driver.

If you select *Native* explicitly with a Firebird 3 client, fbadmin binds the Firebird 4+ types
to legacy types on Firebird 4+ servers (`SET BIND OF INT128 / DECFLOAT / TIME ZONE TO LEGACY`):
values arrive as BIGINT, DOUBLE PRECISION and timestamps without a time zone, and the panel
shows a warning.

**Wire encryption:** the client file sets fbadmin ships (desktop, Docker, `fetch-embedded.py`)
include Firebird's ChaCha plugin, so the native driver negotiates **ChaCha64** with Firebird 4+
servers, like a normal Firebird client (checked with
`RDB$GET_CONTEXT('SYSTEM', 'WIRE_CRYPT_PLUGIN')` against Firebird 4, 5 and the 6 snapshot).
Before 2.1 the file set had no ChaCha plugin and the native driver used Arc4. Firebird 3
servers only offer Arc4.

### pip, desktop app and Docker

- **pip (`pip install firebird-admin`):** installs `firebird-driver` on Python 3.11+. If a
  Firebird 4+ client library is installed (see above), Automatic uses the native driver;
  otherwise the pure-Python one, with nothing else to do. `pip install
  "firebird-admin[native]"` still works; the extra is now empty.
- **Desktop app:** it bundles the Firebird 5 client with the embedded engine, so Automatic
  uses the native driver for remote servers too, through the same library local databases
  use (one libfbclient per process).
- **Docker image:** it contains the Firebird 5.0.4 client for its architecture (amd64 and
  arm64): `libfbclient`, the ChaCha plugin and libtomcrypt from the pinned, SHA-256-checked
  official release (`fetch-embedded.py --client-only`, about 4.7 MB), plus Debian's
  `libtommath1` and ICU (`libicu76`, about 37 MB, needed for time zone names). The library is
  on the system library path, so Automatic finds it and uses the native driver.
  `FBADMIN_DRIVER=firebirdsql` selects the pure-Python driver. Compared with 2.0 the image is
  about 49 MB larger uncompressed (19 MB compressed), most of it ICU.

## What each feature uses

With the **native** driver in use, every connection to a registered server goes through
`firebird-driver`. `backend/fbadmin/native.py` adapts it to the small DB-API subset fbadmin
uses. Errors become the same `firebirdsql` exception classes with the same message format.
Values are made to match (CHAR without trailing blanks, INT128 without a scale as `int`). The
only exception is the event monitor.

| Feature | firebirdsql | native | How (native) |
|---|:-:|:-:|---|
| Object explorer, properties, DDL, schema tools, data grid, dashboards, monitoring (MON$), overview | ✅ | ✅ | `NativeConnection` / `NativeCursor` |
| Query Tool: transactions, isolation levels, read-only, lock timeout | ✅ | ✅ | The raw TPB fbadmin builds becomes firebird-driver's `default_tpb` |
| Query Tool: statement timeout, cancel | ✅ | ✅ | `SET STATEMENT TIMEOUT` (Firebird 4+). Cancel is `DELETE FROM MON$STATEMENTS` from another attachment, as with firebirdsql. The native call releases the GIL, so the cancel request runs at once |
| Query Tool: plans and explained plans | ✅ | ✅ | `Statement.plan` / `Statement.detailed_plan` (firebirdsql: `isc_info_sql_get_plan` / `explain_plan`) |
| Query Tool: parameters, fetch more, statistics, column types | ✅ | ✅ | |
| Data import, data generator, copy data, data compare | ✅ | ✅ | `cursor.prep()` is emulated with a prepared `Statement` |
| PSQL debugger (trace, run, tests) and profiler | ✅ | ✅ | firebirdsql uses wire-level fetches so it keeps the rows sent before an error. The native client hands those rows out before raising the error, so plain cursor fetches suffice |
| Create and drop database | ✅ | ✅ | `create_database` with page size and default character set in the DPB / `drop_database` |
| Services: backup/restore (server file, streamed download, upload restore), nbackup, gstat, validation, sweep, limbo, shutdown/online, properties, server info, server log | ✅ | ✅ | `NativeServices`: firebird-driver's service manager. The three wire primitives fbadmin's service code uses (`_op_service_start`, `_op_service_info`, `_op_response`) map to `IService.start` / `IService.query`, which take the same buffers. The same class serves local databases |
| Firebird users (Services API), trace sessions, replication status | ✅ | ✅ | Same `NativeServices` |
| Wire encryption (per server) | ✅ | ✅ | `WireCrypt = Enabled` / `Disabled` in the connection's configuration (`isc_dpb_config` / `isc_spb_config`) |
| Wire compression (per server) | ✅ | ✅ | `WireCompression = true`. The client needs zlib, which Linux systems have |
| Authentication plugin (per server) | ✅ | ✅ | `AuthClient`. *Srp256* means "Srp256, then Srp", because firebirdsql also falls back to Srp when the server lacks Srp256 (a Firebird 3 default) |
| Connect timeout | ✅ | ✅ | `ConnectionTimeout = 30` |
| INT128, DECFLOAT, NUMERIC(38), TIME/TIMESTAMP WITH TIME ZONE | ✅ | ✅ | With a Firebird 4+ client that can load ICU; a Firebird 3 client gets legacy types (see above) |
| **Event monitor** (POST_EVENT listener) | ✅ | firebirdsql | It speaks the auxiliary-port protocol itself (`events/wire.py`) on top of firebirdsql's socket, so `api/events.py` opens its connection with `driver="firebirdsql"` |
| `fbadmin` command line (backup, restore, sql, ...) | ✅ | ✅ | Follows `FBADMIN_DRIVER` / the saved setting (Automatic by default) |
| Local (embedded) databases | — | always | The embedded engine runs in-process, which only libfbclient can do |

## Correctness: the two drivers compared

`backend/tests/test_driver_equivalence.py` runs the same queries and API calls through both
drivers and requires **identical serialized results**, the JSON the browser receives. It
covers:

- **Types:** every type, with NULLs and extremes. That includes INT128 at both limits,
  NUMERIC(38,6), DECFLOAT(16/34) up to 1E+100 and down to 1E-6143, NaN, ±Infinity and -0,
  TIME/TIMESTAMP WITH TIME ZONE by region and offset, and year 1 / 9999 dates.
- **Character sets and BLOBs:** UTF8 (emoji, CJK), WIN1252, NONE and OCTETS columns, text
  BLOBs in UTF8 and WIN1252, and a 256 KB binary BLOB.
- **Parameters:** values bound as parameters, including a 100,000-character text BLOB and the
  Firebird 4+ types.
- **Query Tool behaviour:** column metadata, fetch more on an open cursor, error messages and
  positions for unknown tables, syntax errors and constraint violations, and plans.
- **Metadata endpoints:** object lists, table DDL, the data grid and row counts.

It runs in the `backend-native` CI job against Firebird 3, 4, 5 and 6. Writing it found
four differences, all fixed in the adapter so the native driver matches firebirdsql:

1. **Error messages.** `fb_interpret` puts a `-` in front of every continuation line of an
   error; firebirdsql does not. The adapter strips it.
2. **CHAR padding.** firebirdsql returns CHAR values without trailing blanks;
   firebird-driver keeps them. The adapter strips them.
3. **INT128 without a scale.** firebird-driver returns a `Decimal` (for example the result
   of `BIGINT / 2` on Firebird 4+); firebirdsql returns an `int`. The adapter converts it.
4. **Exception classes.** The adapter now raises the classes firebirdsql raises for the same
   status codes: `IntegrityError` for constraint violations, `DataError` for arithmetic
   errors, otherwise `OperationalError`. `test_error_classes_match_firebirdsql` checks this.

Two differences remain on the **Firebird 6 snapshot**, both bugs on the firebirdsql side:

- **WIN1252 text BLOBs:** firebirdsql receives them untransliterated and fails to decode them
  ("'utf-8' codec can't decode byte 0xe9").
- **WIN1252 parameters:** a value firebirdsql binds for a WIN1252 column is stored without
  transliteration, so non-ASCII characters come back garbled.

The native driver gets both right. The equivalence test avoids these two cases on Firebird 6
and says why. Firebird 3, 4 and 5 show no difference.

`scripts/driver-compare.py` also compares one value set per type group outside the test
suite; see the "Correctness" line under each benchmark table.

The benchmark found one more firebirdsql limitation:

- **Unclosed cursors on Firebird 5.** firebirdsql keeps a statement allocated on the server
  until its cursor is closed or the connection ends. Firebird 5 shares compiled statements
  and refuses a 1001st concurrent execution of the same request. So one connection that runs
  the same query more than 1000 times without closing its cursors fails with "Too many
  concurrent executions of the same request".
- **Reproduced** with a plain loop: it fails at iteration 1001 on Firebird 5, and passes
  with `cursor.close()` or on Firebird 4.
- **Where it shows in fbadmin:** the catalog code does not close its cursors, so a long
  metadata job on one connection can hit this (loading the 352-object schema below a few
  times did).
- **The native driver** is not affected: the adapter frees a cursor's statement when the
  cursor goes away.
- **The benchmark** therefore opens a fresh connection per metadata run. Closing the cursors
  in `Catalog.rows()` is a separate fix.

## Benchmarks

### How it was measured

`scripts/driver-compare.py` measures both drivers against each server. It uses fbadmin's own
connection layer (`fbadmin.connections.connect`), the same code path the app uses, so the
native numbers include fbadmin's adapter. Each test runs once as a warm-up, then 5 times
(3 times for a test whose warm-up took longer than 5 s). The tables give the median and, in
brackets, the fastest and slowest run. *CPU* is the client process's CPU time, which shows
how much of the wall time Python spends on the client side.

| Test | What it does |
|---|---|
| connect + attach + detach | Open and close one connection (10 runs) |
| round trip ×1000 | `SELECT 1 FROM RDB$DATABASE` 1000 times |
| fetch 10,000 / 100,000 rows | `SELECT *` from a table of INTEGER, BIGINT, VARCHAR(50) UTF8 (non-ASCII), NUMERIC(18,4), DOUBLE, TIMESTAMP, DATE and BOOLEAN; on Firebird 4+ also INT128, DECFLOAT(34) and TIMESTAMP WITH TIME ZONE |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | The same fetch on a plain `firebird.driver` connection, to show the adapter's own cost |
| insert 5,000 rows (params) | One prepared INSERT with 9 parameters, executed 5,000 times, then a commit |
| BLOB write / read 1 MB, 10 MB | Random bytes into a BLOB SUB_TYPE BINARY as a parameter, and reading them back |
| metadata load | What the object tree and the properties panes query (counts, the list of every object type, columns, constraints, indices, triggers and DDL of every table) for a generated schema of 352 objects: 100 tables with a primary key, a foreign key and an index each, 50 views, 50 procedures, 50 triggers, a domain and a sequence. Each run opens a fresh connection (see the cursor note under [Correctness](#correctness-the-two-drivers-compared)) |
| backup stream to local file | A gbak backup through the Services API streamed to stdout (`backup_stream`, used for "download a backup") and written to a local file. The backup holds the 100,000-row table, 9 to 13 MB |

To reproduce (servers as in [DEVELOPMENT.md](DEVELOPMENT.md); takes about two hours, mostly
the firebirdsql runs; `--quick` gives a smoke test in a few minutes):

```bash
scripts/embedded-test-setup.sh       # Firebird 5 client library → build/firebird-embedded
.venv/bin/python scripts/driver-compare.py --ports 3053,3054,3050,3056 \
    --fbclient build/firebird-embedded/lib/libfbclient.so.2 --json driver-compare.json --md driver-compare.md
```

### What the numbers say

These are firebirdsql 1.5.1 numbers; the older ones are compared below
([1.5.0 with 1.4.7](#firebirdsql-150-compared-with-147), [1.5.1 with 1.5.0](#firebirdsql-151)).

- **Row fetches are now faster with firebirdsql on Firebird 4+:** 0.45 to 0.73 times the native
  time. On Firebird 3 native is still 1.2 to 1.4 times faster. Both drivers are CPU-bound in
  Python here, the native one in firebird-driver's ctypes value conversion, which costs more
  than firebirdsql's parser now does. fbadmin's adapter costs little: the "without fbadmin's
  adapter" rows are 1 to 11% faster.
- **Parameterized inserts are faster with firebirdsql** everywhere: 0.53 to 0.64 times the
  native time. firebird-driver's input conversion is not cheap, and firebirdsql 1.5.1 sends
  the execute and its follow-up requests in one round trip.
- **Metadata load costs the same** (0.76 to 1.01 times). It is many small queries.
- **Small round trips are still 3.3 to 4.8 times faster natively.** About 0.23 to 0.29 ms
  instead of 0.85 to 1.1 ms per query.
- **BLOBs and backups are still faster natively:** BLOBs 2.2 to 12 times on Firebird 4+ (13 to
  24 times on Firebird 3, where both use Arc4), backup streaming 1.3 to 3.3 times. Wire
  encryption is done in Python by firebirdsql; 1.5.1 made it much cheaper, but for bulk bytes
  C still wins.
- **Connects cost the same** (22 to 28 ms, 51 to 81 ms on the Firebird 6 snapshot), dominated
  by the server's authentication (SRP).
- **Both drivers used the same cipher** in this run: ChaCha64 on Firebird 4+ (the client library
  used now has the ChaCha plugin, like the file sets fbadmin ships), Arc4 on Firebird 3.

### firebirdsql 1.5.0 compared with 1.4.7

[firebirdsql 1.5.0](https://pypi.org/project/firebirdsql/1.5.0/) brought performance work
([pyfirebirdsql#143](https://github.com/nakagami/pyfirebirdsql/pull/143)): rows are parsed from
a read-ahead buffer without copying, the ChaCha20 XOR works in place, and `executemany` uses
the Firebird 4+ batch API (fbadmin does not call `executemany`). The benchmark was run again
with 1.5.0 the day after the 1.4.7 run, on the same machine and servers. Time saved by 1.5.0
(positive = faster; bold = more than the ±15% run-to-run spread):

| Test | Firebird 3 | Firebird 4 | Firebird 5 | Firebird 6 |
|---|---:|---:|---:|---:|
| connect + attach + detach | +1% | −7% | +9% | −2% |
| round trip ×1000 | **−22%** | −9% | **−19%** | +5% |
| fetch 10,000 rows | **+19%** | +5% | +9% | +11% |
| fetch 100,000 rows | **+17%** | +8% | +14% | +5% |
| insert 5,000 rows (params) | +14% | −12% | +14% | +6% |
| BLOB write 1 MB | **−18%** | −1% | +9% | −1% |
| BLOB read 1 MB | −1% | −12% | +6% | +1% |
| BLOB write 10 MB | **−15%** | −1% | +5% | +7% |
| BLOB read 10 MB | +3% | −5% | −7% | +1% |
| metadata load (352 objects) | +14% | −1% | −7% | +5% |
| backup stream to local file | −5% | **+21%** | +2% | **+20%** |

- **Row fetches are 5 to 19% faster**, the clearest gain, and also the one a back-to-back A/B
  run of both versions (plain driver, no fbadmin code) confirmed: 15 to 20% on Firebird 5.
- **BLOBs, round trips and metadata did not change** beyond the noise: the A/B run showed no
  consistent difference, so the larger swings above (round trips on Firebird 3 and 5, BLOB
  writes on Firebird 3) are run-to-run variation. With encryption on, BLOB and backup speed
  is set by the Python ChaCha20/Arc4 key stream, which 1.5.0 did not change.
- **Backup streaming was about 20% faster** on Firebird 4 and 6, not on 3 and 5.
- The gap to the native driver is therefore about the same as with 1.4.7, a little smaller
  for row fetches.

fbadmin works with both. 1.5.0 needed three adjustments in fbadmin, all guarded so 1.4.x
keeps working:

- `driver_patches.py`: the INT128 / time zone parameter patch recognises 1.5.0's new null
  check (array support) in `params_to_blr`.
- `driver_patches.py`: 1.5.0 asserts the wrong bytes when the description of a statement's
  parameters continues in a second info request (`assert buf2[:2] == bytes([0x08, 0x07])`,
  where the server sends `isc_info_sql_bind, isc_info_sql_describe_vars`, i.e. `[5, 7]`).
  Any statement with about 19 or more parameters failed with `AssertionError`, for example
  "Copy data" into a wide table. fbadmin corrects the assert until the driver does.
- `security/wirecompat.py`: fbadmin's replacement of the driver's packet reader now reads
  1.5.0's read-ahead buffer first (otherwise an error after a fetch, such as a cancelled
  query, blocked), and the new fetch loop gets the same guard against waiting in `select()`
  while decompressed bytes are already buffered (otherwise wire compression with a
  connection timeout stalled).

### firebirdsql 1.5.1

fbadmin requires [firebirdsql 1.5.1](https://pypi.org/project/firebirdsql/1.5.1/) or later. It
carries the fixes fbadmin sent upstream ([pyfirebirdsql#144](https://github.com/nakagami/pyfirebirdsql/pull/144),
[#145](https://github.com/nakagami/pyfirebirdsql/pull/145)), so fbadmin no longer patches them:

- the parameter description continued in a second info request (the wrong assert above);
- `rowcount` after executing a prepared statement: 1.5.1 asks about the statement that was executed,
  in the same round trip as the execute;
- wire compression: compression starts once the whole accept packet is read, the packet reader reads
  the read-ahead buffer first, and no read waits in `select()` while decompressed bytes are buffered.
  `security/wirecompat.py` is gone.

1.5.1 also sends the execute, the first fetch and the row count request of a statement together and
reads the replies afterwards: one round trip instead of two or three.

The benchmark was run again with 1.5.1 (2026-11-12) on the same machine and servers as the 1.5.0
run. Time saved by 1.5.1 (positive = faster), with the native driver's change in the same runs in
brackets as a measure of the noise (on Firebird 4+ the native driver used ChaCha64 this time and
Arc4 before, which explains most of its slowdown there):

| Test | Firebird 3 | Firebird 4 | Firebird 5 | Firebird 6 |
|---|---:|---:|---:|---:|
| connect + attach + detach | −4% (−18%) | −8% (+24%) | −20% (−2%) | −31% (−10%) |
| round trip ×1000 | **+36%** (+4%) | **+71%** (+6%) | **+69%** (−14%) | **+73%** (+6%) |
| fetch 10,000 rows | **+33%** (−18%) | **+82%** (−5%) | **+79%** (+1%) | **+84%** (−6%) |
| fetch 100,000 rows | **+41%** (−11%) | **+83%** (−36%) | **+82%** (−10%) | **+83%** (−16%) |
| insert 5,000 rows (params) | **+22%** (−6%) | **+60%** (−36%) | **+57%** (−14%) | **+63%** (−18%) |
| BLOB write 1 MB | **+65%** (+0%) | **+95%** (−105%) | **+95%** (−8%) | **+95%** (−12%) |
| BLOB read 1 MB | **+72%** (+8%) | **+97%** (−53%) | **+96%** (+0%) | **+96%** (+1%) |
| BLOB write 10 MB | **+66%** (−13%) | **+96%** (−38%) | **+96%** (−8%) | **+95%** (−8%) |
| BLOB read 10 MB | **+83%** (+1%) | **+97%** (−16%) | **+98%** (−4%) | **+97%** (+9%) |
| metadata load (352 objects) | **+20%** (−3%) | **+69%** (−35%) | **+77%** (−3%) | **+65%** (−9%) |
| backup stream to local file | **+71%** (−6%) | **+93%** (−52%) | **+96%** (+10%) | **+94%** (−6%) |

- **Everything that moves data is faster:** row fetches 1.7 to 6 times, BLOBs and backups 3 to 50
  times, most on Firebird 4+ (ChaCha64): the
  faster wire encryption of [#145](https://github.com/nakagami/pyfirebirdsql/pull/145) removes
  most of the client CPU that the key stream used to take.
- **Round trips are 1.6 to 3.7 times faster** (one network round trip per statement instead of
  two or three).
- **Connects did not change** beyond the noise.

The patches for binary and OCTETS parameters, result descriptors on Firebird 6, the Firebird 4+ types
and the service process name remain (`driver_patches.py`); see [upstream-firebirdsql-fb4-types.md](upstream-firebirdsql-fb4-types.md).

### Prepared statements in fbadmin's loops

Most of fbadmin's loops run one statement shape many times:

* data import, copy data, the test data generator and anonymised copies prepare their statement once
  per table (`cursor.prep()`);
* grid edits and data compare scripts use a small cache (`fbadmin/prepared.py`): each distinct SQL
  text is prepared once and executed as often as needed. A grid save of 200 updates prepares one
  UPDATE per set of changed columns. A data compare script prepares one INSERT, UPDATE or DELETE per
  table and column set.

Measured with 2,000 parameterized `UPDATE … WHERE ID = ?` statements in one transaction, each
followed by its row count, as grid edits and data compare check them. The client went through a
TCP proxy that adds 0.5 ms in each direction, to stand for a network. Times are per statement; the
proxy's own overhead makes them higher than on a real network, but the ratio holds.

| Driver | Firebird 3 | Firebird 4 | Firebird 5 |
|---|---|---|---|
| firebirdsql, SQL text | 11.0 ms | 12.0 ms | 11.9 ms |
| firebirdsql, prepared | 6.8 ms (**1.6×**) | 7.3 ms (**1.65×**) | 7.1 ms (**1.67×**) |
| native, SQL text | 6.8 ms | 6.7 ms | 6.6 ms |
| native, prepared | 6.7 ms (1.0×) | 6.7 ms (1.0×) | 6.7 ms (1.0×) |

* **firebirdsql** prepares SQL text again on every execute. A prepared statement saves that round
  trip.
* **The native driver** already reuses the last statement when the same SQL text comes again. In a
  plain loop it gains nothing, but the cache keeps several shapes prepared at once, so mixed
  statements (several tables, several column sets) gain there too.
* What remains with firebirdsql is the execute plus a row-count request (`isc_info_sql_records`),
  which the driver makes after every statement.

firebirdsql 1.5.0 reported a stale `rowcount` after a prepared execute: the count of the cursor's
last *text* statement. fbadmin patches that (`driver_patches.py`, patch 7), because grid edits and
data compare scripts rely on the count to notice rows changed by someone else.

`executemany` (the Firebird 4+ batch API in firebirdsql 1.5) is not used, for two reasons:

* one failing row fails the whole batch, so an import couldn't report and skip single rows;
* its parameter encoding bypasses fbadmin's driver fixes for binary values, INT128 and time zones.

### Caveats

- **One machine, loopback.** Everything ran in one container (4 CPUs) with the servers in
  Docker on the same host, over loopback. Network latency, which would add the same per round
  trip to both drivers, is absent. The client and the servers shared the CPUs.
- **Indicative only.** Spreads were mostly within ±15%. Ratios above about 1.2× are
  meaningful; the connect differences are not.
- **Encryption on, compression off.** These were the defaults. With `WireCrypt = Disabled`
  on the server and encryption off in the registration, firebirdsql's bulk numbers would
  improve a lot. This was not measured: the test servers require encryption.
- **The Firebird 6 numbers are from a pre-release snapshot.**
- **The numbers are single runs of the script** (1.4.7 on 2026-10-24, 1.5.0 on 2026-10-25, 1.5.1 on
  2026-11-12), not a statistical study.

### Results

Environment: Python 3.13.16, Linux-6.18.44-fc-v378-x86_64-with-glibc2.39, 4 CPUs; firebirdsql 1.5.1; firebird-driver 2.0.3 with libfbclient Firebird 5.0 (LI-V6.3.4.1812). Median of 5 runs after 1 warm-up (min–max in brackets); times in ms, lower is better. *ratio* = firebirdsql time / native time.

#### Firebird 3.0.14 (port 3053)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 26.0 (22.5–30.6) | 27.5 (22.9–29.8) | 0.94× | 5.1 | 5.3 |
| round trip ×1000 | 1,118 (973–1,119) | 234 (199–268) | 4.78× | 587 | 91.6 |
| fetch 10,000 rows | 529 (456–603) | 367 (288–465) | 1.44× | 508 | 360 |
| fetch 100,000 rows | 4,576 (4,468–4,889) | 3,717 (3,402–4,218) | 1.23× | 4,326 | 3,671 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 3,748 (3,290–4,156) | | | 3,660 |
| insert 5,000 rows (params) | 2,643 (2,596–2,685) | 4,247 (4,061–4,801) | 0.62× | 1,428 | 2,498 |
| BLOB write 1 MB | 280 (223–296) | 22.2 (19.8–22.8) | 12.60× | 209 | 6.9 |
| BLOB read 1 MB | 254 (227–308) | 12.0 (10.9–15.8) | 21.15× | 224 | 5.6 |
| BLOB write 10 MB | 2,805 (2,736–3,038) | 213 (197–290) | 13.15× | 2,364 | 65.8 |
| BLOB read 10 MB | 2,399 (2,373–2,481) | 98.7 (91.5–106) | 24.31× | 2,194 | 53.2 |
| metadata load (352 objects) | 2,680 (2,528–2,731) | 2,697 (2,580–2,833) | 0.99× | 1,371 | 1,277 |
| backup stream to local file | 2,763 (2,716–2,883) | 831 (741–872) | 3.32× | 1,982 | 76.7 |

Backup size 9.2 MB. Wire: firebirdsql: encrypted=TRUE, compressed=FALSE; native: encrypted=TRUE, compressed=FALSE.

Correctness: 8 of 8 type groups serialize identically.

#### Firebird 4.0.7 (port 3054)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 25.8 (21.9–38.1) | 24.0 (18.7–26.7) | 1.07× | 5.0 | 3.9 |
| round trip ×1000 | 939 (890–998) | 289 (258–329) | 3.25× | 481 | 106 |
| fetch 10,000 rows | 667 (613–714) | 1,221 (1,063–1,387) | 0.55× | 617 | 1,215 |
| fetch 100,000 rows | 5,873 (5,713–6,270) | 12,970 (10,781–13,591) | 0.45× | 5,548 | 12,823 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 11,590 (10,783–12,077) | | | 11,280 |
| insert 5,000 rows (params) | 2,940 (2,574–3,058) | 5,520 (5,184–6,482) | 0.53× | 1,404 | 3,003 |
| BLOB write 1 MB | 115 (89.1–137) | 51.9 (42.4–58.5) | 2.22× | 72.9 | 10.0 |
| BLOB read 1 MB | 89.2 (73.5–108) | 18.8 (15.6–24.3) | 4.75× | 72.8 | 7.4 |
| BLOB write 10 MB | 967 (931–1,094) | 262 (220–306) | 3.69× | 632 | 76.0 |
| BLOB read 10 MB | 1,035 (814–1,098) | 123 (120–164) | 8.43× | 834 | 62.5 |
| metadata load (352 objects) | 2,455 (2,444–2,592) | 3,221 (2,622–5,112) | 0.76× | 1,146 | 1,493 |
| backup stream to local file | 1,944 (1,850–2,031) | 1,512 (1,167–1,748) | 1.29× | 906 | 136 |

Backup size 13.2 MB. Wire: firebirdsql: encrypted=TRUE (ChaCha64), compressed=FALSE; native: encrypted=TRUE (ChaCha64), compressed=FALSE.

Correctness: 12 of 12 type groups serialize identically.

#### Firebird 5.0.4 (port 3050)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 24.2 (21.2–28.9) | 21.9 (19.0–27.0) | 1.10× | 5.1 | 3.8 |
| round trip ×1000 | 900 (804–929) | 266 (226–299) | 3.39× | 492 | 108 |
| fetch 10,000 rows | 716 (524–755) | 986 (871–1,027) | 0.73× | 637 | 970 |
| fetch 100,000 rows | 6,116 (6,110–6,607) | 10,343 (10,290–10,665) | 0.59× | 5,816 | 10,150 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 10,024 (9,031–10,300) | | | 9,430 |
| insert 5,000 rows (params) | 2,789 (2,693–2,926) | 4,326 (4,163–4,685) | 0.64× | 1,417 | 2,557 |
| BLOB write 1 MB | 118 (82.2–123) | 24.5 (23.0–26.6) | 4.80× | 76.0 | 6.0 |
| BLOB read 1 MB | 112 (81.5–122) | 12.4 (11.5–13.3) | 9.06× | 86.8 | 7.1 |
| BLOB write 10 MB | 1,012 (973–1,131) | 203 (192–221) | 4.99× | 664 | 75.0 |
| BLOB read 10 MB | 955 (852–1,120) | 103 (88.0–141) | 9.25× | 764 | 51.2 |
| metadata load (352 objects) | 2,044 (2,016–2,115) | 2,030 (1,972–2,117) | 1.01× | 1,142 | 1,172 |
| backup stream to local file | 1,628 (1,554–1,659) | 724 (694–768) | 2.25× | 926 | 102 |

Backup size 13.1 MB. Wire: firebirdsql: encrypted=TRUE (ChaCha64), compressed=FALSE; native: encrypted=TRUE (ChaCha64), compressed=FALSE.

Correctness: 12 of 12 type groups serialize identically.

#### Firebird 6.0.0 (port 3056)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 68.4 (51.0–81.1) | 57.2 (51.1–73.0) | 1.20× | 6.3 | 4.5 |
| round trip ×1000 | 850 (776–887) | 227 (217–283) | 3.74× | 488 | 90.6 |
| fetch 10,000 rows | 587 (477–627) | 1,026 (869–1,079) | 0.57× | 567 | 1,018 |
| fetch 100,000 rows | 6,541 (5,444–6,918) | 10,940 (9,334–11,185) | 0.60× | 6,275 | 10,852 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 9,763 (9,554–10,928) | | | 9,337 |
| insert 5,000 rows (params) | 2,760 (2,549–3,134) | 4,293 (4,246–4,511) | 0.64× | 1,406 | 2,490 |
| BLOB write 1 MB | 120 (96.8–129) | 27.1 (24.4–29.9) | 4.42× | 58.3 | 8.1 |
| BLOB read 1 MB | 105 (84.7–108) | 11.3 (9.6–19.1) | 9.32× | 82.6 | 5.4 |
| BLOB write 10 MB | 1,119 (997–1,186) | 200 (190–205) | 5.59× | 672 | 61.4 |
| BLOB read 10 MB | 1,061 (928–1,142) | 90.6 (85.8–107) | 11.71× | 869 | 56.9 |
| metadata load (352 objects) | 2,794 (2,731–2,877) | 2,765 (2,710–2,821) | 1.01× | 1,239 | 1,241 |
| backup stream to local file | 1,664 (1,558–1,738) | 762 (742–826) | 2.19× | 916 | 94.4 |

Backup size 13.1 MB. Wire: firebirdsql: encrypted=TRUE (ChaCha64), compressed=FALSE; native: encrypted=TRUE (ChaCha64), compressed=FALSE.

Correctness: 12 of 12 type groups serialize identically.

## Tests and CI

- **Locally:** see [DEVELOPMENT.md](DEVELOPMENT.md#native-driver-tests). In short:
  `FBADMIN_FBCLIENT=build/firebird-embedded/lib/libfbclient.so.2 python -m pytest -q` runs the
  whole backend suite with the default setting, which then resolves to the native driver;
  `FBADMIN_DRIVER=firebirdsql` forces the pure-Python one.
- **CI:** the plain `backend` jobs run the whole suite with the pure-Python driver
  (`FBADMIN_DRIVER=firebirdsql`). Their runner image (Ubuntu 26.04) ships `libfbclient2`, a
  Firebird 4 client, so Automatic itself resolves to native there, as it does for users on
  Ubuntu 25.04+ and Debian 13; `test_driver_auto.py` asserts it
  (`FBADMIN_TEST_EXPECT_DRIVER=native`). One more job runs the native driver with that system
  Firebird 4 client against a Firebird 5 server. The `backend-native` job of
  `.github/workflows/ci.yml` runs the full suite with `FBADMIN_DRIVER=native` against
  Firebird 3, 4, 5 and the 6 snapshot, and checks that Automatic resolves to native with that
  client (`FBADMIN_TEST_EXPECT_DRIVER=native`) and negotiates ChaCha64. Firebird 6 is
  reported but does not fail the build. The client library is the Firebird 5 one from the
  pinned, SHA-256-checked embedded file set (`scripts/embedded-test-setup.sh`); the release
  archive is cached between runs. The `e2e` job runs the Playwright suite with the default
  setting (Automatic, which resolves to native with the embedded file set's client) and with
  `FBADMIN_DRIVER=firebirdsql`. `e2e/tests/driver.spec.ts` covers the settings panel,
  switching to each driver and back, and an axe audit.
- **Automatic selection:** `backend/tests/test_driver_auto.py` covers the resolution rules
  (package missing, no library, Firebird 3 client, Firebird 4+ client, no ICU, environment
  and saved settings, explicit native refused when unavailable) and the run-time fallback (a
  client-library failure falls back, a Firebird error such as a wrong password does not), also
  against a real server with a simulated `OSError` from the client library.
- **Driver-specific tests:** `backend/tests/test_native_driver.py` covers the setting and its
  API, the environment override, switching at run time (pools closed, open Query Tool
  sessions kept), Query Tool sessions, services, wire options, error classes, 1 and 10 MB
  BLOBs, Firebird 4+ types (or legacy binding with a Firebird 3 client), and the event
  monitor staying on firebirdsql.

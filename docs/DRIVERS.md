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

- **Wire encryption dominates for firebirdsql.** Firebird encrypts the wire by default
  (`WireCrypt = Required` in the Docker images used here), and firebirdsql encrypts and
  decrypts in pure Python. Profiling a 1 MB BLOB read and write showed nearly all client time
  in `firebirdsql/chacha.py`. The firebirdsql CPU column is almost equal to its wall time in
  the bulk tests, so the client CPU is the bottleneck, not the server or the loopback network.
  That explains the extremes for anything that moves bytes: BLOBs are 25 to 380 times faster
  with the native driver, and backup streaming 13 to 52 times.
- **ChaCha is the costlier cipher.** On Firebird 4+ firebirdsql negotiates ChaCha64, and the
  gap is largest there. On Firebird 3 both drivers use Arc4, and firebirdsql is "only" 26 to
  160 times slower for BLOBs.
- **The two drivers did not use the same cipher on Firebird 4+ in these runs.** The native
  driver used Arc4 there too, because the embedded file set had no ChaCha plugin at the time.
  The file sets now include it (see [Which client library](#which-client-library)), so the
  native driver negotiates ChaCha64 like firebirdsql. In C that costs about the same: writing
  and reading a 10 MB BLOB on Firebird 5 took 443 / 116 ms with ChaCha64 against 436 / 139 ms
  with Arc4 (median of 5, same machine, within the run-to-run spread).
- **Row fetches are 3 to 5 times faster natively.** Both drivers are CPU-bound in Python
  here, the native one in firebird-driver's ctypes value conversion. fbadmin's adapter costs
  little: the "without fbadmin's adapter" rows are within the run-to-run spread on Firebird
  4 to 6 and about 12% faster on Firebird 3.
- **Small round trips are 6 to 14 times faster natively.** About 0.2 to 0.3 ms instead of 1.4
  to 3.3 ms per query.
- **Connects cost the same** (20 to 25 ms, 50 ms on the Firebird 6 snapshot), dominated by
  the server's authentication (SRP).
- **Parameterized inserts gain least** (1 to 2 times). Each execute is a server round trip
  either way; firebird-driver's input conversion is not cheap.
- **Metadata load is 1.6 to 3.3 times faster natively.** It is many small queries, so it
  sits between round trips and fetches.

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
- **The numbers are a single run of the script** on 2026-10-24, not a statistical study.

### Results

Environment: Python 3.13.16, Linux-6.18.44-fc-v153-x86_64-with-glibc2.39, 4 CPUs; firebirdsql 1.4.7; firebird-driver 2.0.3 with libfbclient Firebird 5.0 (LI-V6.3.4.1812). Median of 5 runs after 1 warm-up (min–max in brackets); times in ms, lower is better. *ratio* = firebirdsql time / native time.

#### Firebird 3.0.14 (port 3053)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 25.3 (22.2–37.0) | 22.5 (21.4–23.8) | 1.12× | 4.6 | 4.4 |
| round trip ×1000 | 1,424 (1,363–1,483) | 222 (185–263) | 6.43× | 635 | 88.5 |
| fetch 10,000 rows | 971 (904–1,025) | 265 (249–298) | 3.67× | 935 | 259 |
| fetch 100,000 rows | 9,304 (9,107–9,407) | 2,860 (2,700–3,124) | 3.25× | 8,965 | 2,824 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 2,515 (2,452–2,628) | | | 2,484 |
| insert 5,000 rows (params) | 3,956 (3,811–4,318) | 3,865 (3,717–4,219) | 1.02× | 1,707 | 2,071 |
| BLOB write 1 MB | 676 (652–717) | 25.9 (25.4–35.6) | 26.15× | 634 | 7.9 |
| BLOB read 1 MB | 905 (841–953) | 12.0 (10.8–14.3) | 75.21× | 562 | 6.1 |
| BLOB write 10 MB | 7,152 (7,064–7,385) | 210 (196–247) | 34.05× | 6,593 | 67.1 |
| BLOB read 10 MB | 14,578 (14,432–15,419) | 91.2 (88.7–96.1) | 159.82× | 10,488 | 52.8 |
| metadata load (352 objects) | 3,925 (3,836–4,053) | 2,445 (2,380–2,522) | 1.61× | 1,742 | 1,123 |
| backup stream to local file | 9,189 (8,684–9,282) | 703 (665–753) | 13.08× | 8,360 | 84.9 |

Backup size 9.2 MB. Wire: firebirdsql: encrypted=TRUE, compressed=FALSE; native: encrypted=TRUE, compressed=FALSE.

Correctness: 8 of 8 type groups serialize identically.

#### Firebird 4.0.7 (port 3054)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 22.2 (20.2–35.4) | 24.7 (21.4–30.1) | 0.90× | 4.5 | 4.4 |
| round trip ×1000 | 2,929 (2,605–3,062) | 298 (270–520) | 9.83× | 1,603 | 114 |
| fetch 10,000 rows | 3,863 (3,614–3,949) | 713 (692–962) | 5.41× | 3,765 | 698 |
| fetch 100,000 rows | 38,063 (36,345–39,475) | 7,585 (7,353–8,019) | 5.02× | 37,257 | 7,467 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 8,036 (7,645–8,146) | | | 7,217 |
| insert 5,000 rows (params) | 6,542 (6,499–6,953) | 4,148 (3,796–4,315) | 1.58× | 3,704 | 2,123 |
| BLOB write 1 MB | 2,274 (2,192–2,327) | 29.2 (27.0–30.0) | 77.92× | 2,198 | 9.1 |
| BLOB read 1 MB | 2,698 (2,577–3,010) | 16.8 (14.1–17.9) | 160.73× | 2,243 | 8.3 |
| BLOB write 10 MB | 22,835 (22,716–23,407) | 215 (185–239) | 106.28× | 22,121 | 66.4 |
| BLOB read 10 MB | 34,498 (33,390–35,133) | 93.6 (85.3–103) | 368.67× | 28,930 | 58.2 |
| metadata load (352 objects) | 7,863 (7,398–8,495) | 2,618 (2,475–2,687) | 3.00× | 5,023 | 1,205 |
| backup stream to local file | 36,898 (36,545–37,094) | 957 (919–1,006) | 38.56× | 35,471 | 112 |

Backup size 13.2 MB. Wire: firebirdsql: encrypted=TRUE (ChaCha64), compressed=FALSE; native: encrypted=TRUE (Arc4), compressed=FALSE.

Correctness: 12 of 12 type groups serialize identically.

#### Firebird 5.0.4 (port 3050)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 22.0 (18.1–31.7) | 25.4 (22.0–36.4) | 0.87× | 4.6 | 4.6 |
| round trip ×1000 | 2,451 (2,338–2,551) | 317 (256–365) | 7.72× | 1,432 | 115 |
| fetch 10,000 rows | 3,685 (3,479–4,448) | 859 (770–1,117) | 4.29× | 3,614 | 826 |
| fetch 100,000 rows | 40,368 (37,625–41,961) | 9,270 (8,758–11,243) | 4.35× | 39,566 | 9,086 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 9,452 (9,046–9,463) | | | 9,001 |
| insert 5,000 rows (params) | 7,551 (7,335–8,065) | 4,949 (4,779–5,529) | 1.53× | 4,059 | 2,546 |
| BLOB write 1 MB | 2,463 (2,398–2,621) | 30.5 (26.4–31.5) | 80.84× | 2,391 | 8.9 |
| BLOB read 1 MB | 3,042 (2,845–3,286) | 13.9 (13.1–14.4) | 219.15× | 2,490 | 7.8 |
| BLOB write 10 MB | 24,580 (23,885–24,735) | 268 (245–334) | 91.88× | 23,918 | 77.9 |
| BLOB read 10 MB | 36,634 (36,555–37,248) | 124 (104–144) | 296.26× | 30,637 | 65.3 |
| metadata load (352 objects) | 8,306 (8,290–8,420) | 2,505 (2,349–2,702) | 3.32× | 5,604 | 1,247 |
| backup stream to local file | 39,478 (37,606–40,281) | 756 (724–782) | 52.21× | 38,120 | 123 |

Backup size 13.1 MB. Wire: firebirdsql: encrypted=TRUE (ChaCha64), compressed=FALSE; native: encrypted=TRUE (Arc4), compressed=FALSE.

Correctness: 12 of 12 type groups serialize identically.

#### Firebird 6.0.0 (port 3056)

| Test | firebirdsql | native | ratio | firebirdsql CPU | native CPU |
|---|---:|---:|---:|---:|---:|
| connect + attach + detach | 51.2 (46.3–69.5) | 49.8 (44.5–56.4) | 1.03× | 5.4 | 4.2 |
| round trip ×1000 | 3,303 (3,169–3,398) | 231 (226–276) | 14.27× | 1,834 | 91.2 |
| fetch 10,000 rows | 4,009 (3,936–4,137) | 757 (741–792) | 5.30× | 3,955 | 742 |
| fetch 100,000 rows | 40,154 (40,077–41,287) | 7,568 (7,438–7,887) | 5.31× | 39,509 | 7,472 |
| fetch 100,000 rows, firebird-driver without fbadmin's adapter | | 7,804 (7,571–7,831) | | | 7,511 |
| insert 5,000 rows (params) | 7,919 (7,768–8,372) | 4,073 (3,898–4,146) | 1.94× | 4,340 | 2,101 |
| BLOB write 1 MB | 2,457 (2,315–2,564) | 22.2 (20.6–26.5) | 110.63× | 2,381 | 7.2 |
| BLOB read 1 MB | 3,040 (2,840–3,185) | 9.4 (8.9–9.7) | 323.77× | 2,465 | 5.2 |
| BLOB write 10 MB | 24,470 (23,598–25,595) | 222 (213–317) | 110.17× | 23,609 | 64.7 |
| BLOB read 10 MB | 35,889 (35,463–36,176) | 94.8 (83.5–108) | 378.73× | 29,775 | 56.7 |
| metadata load (352 objects) | 8,474 (8,439–8,703) | 2,979 (2,803–3,029) | 2.84× | 5,300 | 1,255 |
| backup stream to local file | 36,829 (35,960–36,926) | 798 (762–821) | 46.16× | 35,500 | 117 |

Backup size 13.1 MB. Wire: firebirdsql: encrypted=TRUE (ChaCha64), compressed=FALSE; native: encrypted=TRUE (Arc4), compressed=FALSE.

Correctness: 12 of 12 type groups serialize identically.

## Tests and CI

- **Locally:** see [DEVELOPMENT.md](DEVELOPMENT.md#native-driver-tests). In short:
  `FBADMIN_FBCLIENT=build/firebird-embedded/lib/libfbclient.so.2 python -m pytest -q` runs the
  whole backend suite with the default setting, which then resolves to the native driver;
  `FBADMIN_DRIVER=firebirdsql` forces the pure-Python one.
- **CI:** the plain `backend` jobs install `firebird-driver` (a regular dependency) but have no
  client library, so Automatic resolves to firebirdsql there; `test_driver_auto.py` asserts it
  (`FBADMIN_TEST_EXPECT_DRIVER=firebirdsql`). The `backend-native` job of
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

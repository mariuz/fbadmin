# Local databases: the Firebird embedded engine

Roadmap 1.2: *"Embedded engine in the desktop app: bundle Firebird embedded so a local .fdb
opens without a running server."*

**Outcome.** Bundling is sound, and it is built:

- **Linux x86_64: done and verified.** That covers the development mode
  (`FBADMIN_EMBEDDED_DIR`) and a PyInstaller build made locally with the engine bundled. The
  backend tests, an end-to-end Playwright spec and a smoke test of the frozen app pass.
- **Windows x64 and macOS (arm64, x86_64): packaged and smoke-tested in CI.** The desktop
  workflow runs the same smoke test on the frozen app on every platform: it creates a local
  database, writes Unicode data, queries it, backs it up and restores it. It passed on all
  four platforms and is required for a release. See [Open items](#unverified-and-open-items).

Local database files open inside the fbadmin process. They support browsing, DDL, the data
grid, the Query Tool, monitoring, and backup/restore, gstat, validation, sweep, properties
and nbackup. Trace, user management and encryption are hidden for them, or fail with a
clear reason.

## How Firebird embedded works

Since Firebird 3 there is no separate embedded library. The ordinary client library
(`libfbclient.so` / `fbclient.dll` / `libfbclient.dylib`) loads *providers* listed in
`firebird.conf` (`Providers = Remote, Engine13, Loopback`):

- **Remote** handles connection strings with a host (`host:/path`, `inet://…`).
- **Engine13** (`plugins/libEngine13.so`, `engine13.dll`) is the database engine itself. It
  opens a plain file path inside the calling process. This is "embedded".
- **Loopback** retries a host-less string over TCP to `localhost`.

The library finds its configuration, messages and plugins in its *root directory*: the
`FIREBIRD` environment variable or the directory of the library. Character sets come from
`intl/fbintl` with `fbintl.conf`. Time zones and Unicode collations use ICU plus the
`tzdata/*.res` files. The embedded engine does no authentication: the user name in the
connection request becomes `CURRENT_USER`, and SYSDBA has all rights.

The pure-Python driver `firebirdsql` speaks the wire protocol, so it can never open a file
in-process. Local databases therefore always go through `firebird-driver` (the native adapter
in `backend/fbadmin/native.py`) on top of the bundled libfbclient. With the file set present,
the default driver setting (*Automatic*) uses the same native driver and library for servers
too (see [DRIVERS.md](DRIVERS.md)).

## The file set (Firebird 5.0.4)

`packaging/embedded/fetch-embedded.py` takes the following files from the official release
archives and nothing else (no server binaries, tools, examples or security database):

| | Linux x64 | Windows x64 | macOS arm64 / x64 |
|---|---|---|---|
| client | `lib/libfbclient.so.2` | `fbclient.dll` | `lib/libfbclient.dylib` |
| engine | `plugins/libEngine13.so` | `plugins/engine13.dll` | `plugins/libEngine13.dylib` |
| profiler plugin (RDB$PROFILER) | `plugins/libDefault_Profiler.so` | `plugins/default_profiler.dll` | `plugins/libDefault_Profiler.dylib` |
| wire encryption plugin (ChaCha64/ChaCha, for remote servers) | `plugins/libChaCha.so` | `plugins/chacha.dll` | `plugins/libChaCha.dylib` |
| character sets | `intl/fbintl`, `intl/fbintl.conf` | `intl/fbintl.dll`, `.conf` | `intl/libfbintl.dylib`, `.conf` |
| crypto / bignum | `lib/libtomcrypt.so.1`, `lib/libtommath.so.1` (from the build machine) | linked statically | `lib/libtomcrypt.dylib`, `lib/libtommath.dylib` |
| ICU | the system's (any version 3.8 to 79) | `icuuc63.dll`, `icuin63.dll`, `icudt63.dll`, `icudt63l.dat` | `lib/libicuuc.71.1.dylib`, `lib/libicui18n.71.1.dylib`, `lib/libicudata.71.dylib` |
| runtime | glibc ≥ 2.17 class | `msvcp140*.dll`, `vcruntime140*.dll`, `zlib1.dll` | system |
| common | `firebird.conf`, `plugins.conf`, `firebird.msg`, `tzdata/*.res` (+ `ids.dat` on Windows/macOS), licences | | |
| **size** (raw / gzip) | **19.0 / 7.3 MB** | **45.9 / 17.9 MB** | **53.9 / 19.9 MB** (arm64) |

Most of the size is the engine (~10 MB) and, on Windows and macOS, ICU's data (27–30 MB).
The ChaCha plugin (about 1 MB) is there for the native driver's connections to servers: without
it the client negotiates only Arc4 with Firebird 4+ servers. `--client-only` (Linux, used by
the Docker image) extracts just the client part: `libfbclient`, libtomcrypt, the ChaCha
plugin, `firebird.msg`, `plugins.conf` and the licences (4.7 MB); `--platform linux-arm64`
takes the arm64 release archive (SHA-256 pinned like the others, and equal to the value
SourceForge publishes for its copy).
The Linux frozen app (`dist/fbadmin`) is 122 MB on disk with the engine, about 18 MB more
than without it.

How the list was established:

- **Linux.** `ldd` and `readelf` on the extracted tarball. `libfbclient` has
  `RPATH=$ORIGIN/../lib`. The engine and fbintl need `libfbclient.so.2`, `libtomcrypt.so.1`
  and **`libtommath.so.1`**, which the tarball does not ship: its `install.sh` takes it from
  the distribution. The fetch script copies it from the build machine (`apt install
  libtommath1`; Unlicense). ICU is loaded with `dlopen("libicuuc.so.NN")` by probing
  versions (see `src/common/unicode_util.cpp`), so the system ICU is used. WebKitGTK depends
  on ICU, so every machine that can show the desktop window has one. Files are stored under
  their sonames as real files, without symlinks.
- **Windows.** A PE import-table scan: `fbclient.dll`, `engine13.dll` and `fbintl.dll` import
  only the MSVC runtime and `fbclient.dll`, and libtom is linked statically. Firebird's
  Windows module loader resolves relative module names (ICU) against its root
  (`src/common/os/win32/mod_loader.cpp`), and ctypes loads `fbclient.dll` with its own
  directory on the DLL search path.
- **macOS.** The release has no tarball, only `.pkg` installers. The script reads the xar
  container and the cpio payload in pure Python. Two quirks come from the Mach-O load
  commands and the source:
  1. The libraries reference `@rpath/lib/…` but carry no `LC_RPATH`. Firebird's executables
     provide `@loader_path/..`. On a Mac the script adds that rpath with
     `install_name_tool`, redirects ICU's internal `libicuuc.71.dylib` reference to the
     shipped file, and re-signs the dylibs ad hoc.
  2. The engine `dlopen()`s ICU as the *relative* path `lib/libicuuc.71.1.dylib`
     (`unicode_util.cpp` + `os/darwin/mod_loader.cpp`), which resolves against the working
     directory. fbadmin therefore `chdir`s to the engine root on macOS when it prepares the
     client library.

### Release, checksums, download

The release is pinned to **Firebird 5.0.4 (5.0.4.1812)**, the current 5.0 release. The
SHA-256 of each archive is pinned in `fetch-embedded.py`. They were computed from copies
downloaded from the SourceForge mirror. Each archive's SHA-1 equals the value SourceForge
publishes in its file metadata, and a second download of the Linux archive from the GitHub
release URL produced the same SHA-256. The script downloads from the GitHub release, falls
back to the SourceForge mirror, and refuses any archive whose SHA-256 differs. To move to a
new release, update `VERSION`, `BUILD` and the four hashes, and check the file lists, ICU
versions included.

## Licences and redistribution

- **Firebird** is licensed under the *InterBase Public License 1.0* (`IPLicense.txt`) and
  the *Initial Developer's Public License 1.0* (`IDPLicense.txt`). Both are MPL 1.1
  derivatives that allow distributing the executables, also inside a larger work under
  another licence (§3.6, §3.7), for free and commercially. Distributing them requires
  shipping the licences and a notice saying where the source code is available.
  `packaging/embedded/NOTICE.txt` does that and is copied into every bundle under
  `firebird/licenses/`. fbadmin does not change Firebird code. On macOS only an rpath is
  added and the files are re-signed, which the notice states.
- **ICU** (Windows 63, macOS 71) is under the Unicode licence (`ICU-63.txt`, `ICU-71.txt`).
  **LibTomMath and LibTomCrypt** are public domain / Unlicense. **zlib** (Windows) is under
  the zlib licence. All of these are vendored in `packaging/embedded/licenses/` and copied
  per platform. The **MSVC runtime** DLLs are redistributed as Firebird ships them, under
  Microsoft's redistribution terms.
- fbadmin's own MIT licence does not cover these files. The notice says so.

## What works on a local database, and how

| Feature | Local databases | How |
|---|---|---|
| Object explorer, properties, DDL extraction, schema tools | ✅ | Metadata connections use `NativeConnection` (the same adapter remote servers use with the native driver, see [DRIVERS.md](DRIVERS.md)) |
| Create / open / drop database | ✅ | `firebird-driver` `create_database` / `connect` / `drop_database` |
| Data grid (view, edit, insert, delete) | ✅ | Same metadata connection |
| Query Tool (transactions, isolation, read-only, lock and statement timeouts, plans, parameters, fetch more, statistics, cancel) | ✅ | `query.py` calls three hooks when present: `set_tpb` (the raw TPB `TxOptions` builds becomes firebird-driver's `default_tpb`), `end_transaction`, and `plan` (`Statement.plan` / `detailed_plan`). Cancel uses `DELETE FROM MON$STATEMENTS` from another attachment, as on servers |
| Monitoring (MON$), dashboards, attachment stats | ✅ | Ordinary SQL |
| Backup/restore (server file, streamed download, upload restore), nbackup, validation, sweep, limbo, shutdown/online, properties, gstat, server info | ✅ | `EmbeddedServices` (a `NativeServices`, the class remote servers use with the native driver, which subclasses `firebirdsql.services.Services`) maps the three wire primitives the maintenance code uses (`_op_service_start`, `_op_service_info`, `_op_response`) to `IService.start` / `IService.query` of the embedded service manager, which take the same parameter buffers. All existing maintenance code runs unchanged |
| Trace | ❌ hidden | The trace plugin is not bundled. Service actions 22–26 raise "Trace is not available for local (embedded) databases" |
| Firebird users / mappings | ❌ hidden | The embedded engine does not authenticate, and there is no security database or user manager in the file set |
| Encryption | ❌ hidden | No crypt plugin is bundled |
| Replication, wire encryption/compression, auth plugin | n/a | There is no network |

Verified on Linux by `backend/tests/test_embedded.py` (above) and by
`packaging/embedded/smoke-test.py` against the frozen app.

### Concurrency: one process owns the file

The embedded engine runs in SuperServer mode (`ServerMode = Super`, the default), which
**opens the database file exclusively** for the process:

- Inside fbadmin any number of attachments work: the tree's metadata pool, data grids, the
  Query Tool tabs, and the service manager (backup and restore attach in the same process).
- While fbadmin has the file open, no other process can open it: another fbadmin, `isql` in
  embedded mode, or a Firebird server. The reverse is also true. The attempt fails with
  `I/O error during "lock" operation for file "…"`. fbadmin rewrites that message to
  "*… is in use by another program (a Firebird server or another fbadmin)*" (tested both
  ways in `test_file_is_exclusive_to_the_process`).
- When fbadmin disconnects the database (or exits), the file is released and others can
  open it.
- Lock files go to `<data dir>/embedded/lock` (`FIREBIRD_LOCK`), not `/tmp/firebird`, so a
  system Firebird server's lock directory with its owner and permissions never gets in the
  way. Exclusivity does not depend on the lock directory: it is a file lock on the database.

### Security model

- The engine runs with fbadmin's operating-system rights and authenticates nobody, so local
  databases are **never available in multi-user server mode** (`FBADMIN_SERVER_MODE`).
  `FBADMIN_EMBEDDED=0` turns them off anywhere.
- Every local connection forces `Providers = Engine13` in its DPB/SPB. A local registration
  can never fall back to `Loopback` and reach a server on `localhost` by accident.
- Paths must be absolute local file paths. `host:path`, `inet://`, `xnet://`, UNC paths and
  aliases are rejected.
- The generic server API cannot turn a server into an embedded one: `embedded` is not part
  of `ServerIn`/`ServerUpdate`.

### Signals

On Unix, libfbclient installs SIGINT/SIGTERM handlers that shut the engine down, and chains
to a previous handler **only if one exists** (`src/yvalve/why.cpp`,
`src/common/os/posix/isc_ipc.cpp`). In the desktop shell SIGTERM had the default action, so
`kill <pid>` closed the attachments but left the process running. The shell now installs its
own SIGTERM handler at startup: it closes the window or leaves the wait loop. Firebird
chains to it, and the process exits cleanly (checked: about 3 s, including the engine
shutdown). The `fbadmin` server command was already fine because uvicorn installs handlers
before the library loads.

## Design

- **Where the engine comes from** (`backend/fbadmin/embedded.py`):
  1. `FBADMIN_EMBEDDED_DIR`, an extracted file set.
  2. Otherwise `<PyInstaller bundle>/firebird`.

  `prepare_client()` sets `FIREBIRD` (the root) and `FIREBIRD_LOCK`, and points
  firebird-driver at the bundled libfbclient. With an engine present, that one library also
  serves remote native connections (the "native" database driver, see [DRIVERS.md](DRIVERS.md)), because a process can load
  only one libfbclient. `GET /api/embedded/status` reports availability and the reason when
  local databases are unavailable.
- **Registrations.** Local databases are `DatabaseEntry`s under a single `ServerEntry` with
  `embedded=True`, named "Local databases" and created on first use. Every existing
  endpoint then works unchanged:
  - `connections.connect()` sends such entries to `embedded.connect_local()`.
  - `ConnectionManager.service()` sends them to `embedded.services()`.
  - `password_for()` returns a placeholder, since there is no authentication.
- **API.** `POST /api/embedded/databases {path, name?, create, page_size, charset}` creates
  or opens a file and registers it. Opening checks that the file is a Firebird database no
  other process holds. A file registered twice returns the existing entry.
- **UI** (`frontend/src/features/embedded`):
  - A **Local database** top-bar button (shown only when available) opens the
    create/open dialog, with a native **Browse…** in the desktop window
    (`window.pywebview.api.choose_database_file`, `DesktopApi` in `desktop.py`).
  - The "Local databases" node offers *Create local database…* / *Open local database
    file…*.
  - A menu filter (`registerMenuFilter`, new in `registry.tsx`) hides the server-only
    items on that node: edit server, register/create database, trace, encryption,
    Firebird users.
- **Settings:** `FBADMIN_EMBEDDED_DIR` and `FBADMIN_EMBEDDED=0`. New databases default to
  `<data dir>/databases/`.

## Development and tests

```bash
scripts/embedded-test-setup.sh                 # -> build/firebird-embedded (Linux x64, macOS)
# firebird-driver is a dependency of fbadmin (Python 3.11+)
FBADMIN_TEST_EMBEDDED_DIR=$PWD/build/firebird-embedded \
  .venv/bin/python -m pytest -q backend/tests/test_embedded.py
FBADMIN_EMBEDDED_DIR=$PWD/build/firebird-embedded .venv/bin/python -m fbadmin   # try it in the UI
```

`backend/tests/test_embedded.py` (no Firebird server needed):

- Path validation, unavailable and turned-off states, and that the generic API cannot set
  `embedded`.
- An end-to-end test: create a database, DDL (identity, UNICODE_CI, time zones, a
  procedure), the data grid, the Query Tool (parameters, plans, statistics,
  rollback/commit, snapshot + read-only TPB, statement timeout, error positions, fetch
  more, cancel) and monitoring.
- Backup/restore to a file, streamed backup + upload restore, the older `servers.py`
  backup, validation, sweep, statistics, properties, trace refused with a reason, drop
  database.
- Open existing, duplicates, missing and non-database files.
- Cross-process exclusivity in both directions.
- The desktop file-dialog bridge.

`e2e/tests/embedded.spec.ts` drives the dialog, the tree and the Query Tool in Chromium. It
skips itself when the server has no engine.

CI (`ci.yml`):

- The Firebird 5 backend job runs `scripts/embedded-test-setup.sh` and `test_embedded.py`.
- The Playwright job starts fbadmin with `FBADMIN_EMBEDDED_DIR`.

`desktop.yml` fetches the platform's file set before PyInstaller (with
`FBADMIN_REQUIRE_EMBEDDED=1`) and runs `packaging/embedded/smoke-test.py` against the frozen
app.

## Packaging

- `packaging/embedded/fetch-embedded.py --platform {linux-x64,windows-x64,macos-arm64,macos-x64}
  --dest build/firebird-embedded` (stdlib only; `--archive FILE` for an offline copy;
  `--libtommath PATH` on Linux).
- `packaging/pyinstaller/fbadmin.spec` copies `build/firebird-embedded` (or
  `FBADMIN_EMBEDDED_BUNDLE`) verbatim to `<app>/firebird` as data, so PyInstaller does not
  rewrite the libraries. It also collects `firebird.driver`/`firebird.base`. Without a file
  set the app builds as before, minus local databases.
- The macOS signing step signs the bundled dylibs explicitly, because `codesign --deep`
  does not reach Mach-O files among data. `entitlements.plist` already has
  `disable-library-validation`.
- The Linux `.deb`/`.rpm` need no new dependency: ICU comes in through WebKitGTK, and
  libtommath is bundled. The **Flatpak is not covered**: it pip-installs the wheel and has
  no PyInstaller bundle. It could add the Linux file set as a module later.

## Unverified and open items

- **Windows and macOS are tested only by the CI smoke test** of the frozen app (create,
  query, backup and restore of a local database), which passes. The installed MSI and dmg
  and a notarized build have not been exercised with local databases. Areas to watch:
  - Windows: DLL resolution of the MSVC runtime next to PyInstaller's own copy.
  - macOS:
    - the `install_name_tool`/ad-hoc-signing fix-ups;
    - the relative ICU `dlopen` + `chdir`;
    - where PyInstaller's `BUNDLE` places Mach-O data files;
    - notarization of the bundled dylibs.
- The packaged builds are not verified beyond the local Linux PyInstaller build: the
  AppImage/deb/rpm conversion, the MSI and the dmg were not built here.
- The Linux bundle relies on the host's ICU. A minimal system without ICU would fail with
  `isc_icu_library`, but WebKitGTK, which the window needs, already depends on ICU.
- Only Firebird 5 is bundled, so the rules of a Firebird 5 server apply:
  - Firebird 4 files (ODS 13.0) open; Firebird 5 upgrades the minor ODS.
  - Firebird 3 files (ODS 12) do not open: restore a backup instead.
  - Not tested here.

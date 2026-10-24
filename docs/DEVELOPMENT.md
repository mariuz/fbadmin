# Development and testing

## Setup

```bash
git clone https://github.com/mariuz/fbadmin && cd fbadmin
python -m venv .venv && . .venv/bin/activate
pip install -e "backend[dev]"
(cd frontend && npm install && npm run build)
scripts/test-servers.sh          # Firebird 5/3/4 in Docker on ports 3050/3053/3054
```

Run the app with auto-reload, and the UI with hot reload:

```bash
fbadmin --reload --no-browser --log-level debug
cd frontend && npm run dev       # http://localhost:5173, proxies /api to :5050
```

## Backend tests

```bash
cd backend && python -m pytest -q                         # Firebird 5 on :3050
FBADMIN_TEST_PORT=3053 python -m pytest -q                 # Firebird 3
FBADMIN_TEST_PORT=3054 python -m pytest -q                 # Firebird 4
python -m pytest -q -m "not slow"                          # skip the slow property tests
ruff check backend
```

Integration tests skip when no server is reachable. `FBADMIN_TEST_HOST`, `_PORT`,
`_PASSWORD` and `_DIR` (a directory on the server) point them elsewhere.

### Fuzz and property tests (hypothesis)

- `tests/test_sqlsplit_fuzz.py` fuzzes the SQL splitter and parameter scanner: random
  text never crashes it and statement offsets/lines/columns always point into the script;
  generated scripts (strings with terminators, quoted identifiers, comments, nested
  `BEGIN … END` and `CASE … END`, `SET TERM`) split back into exactly the statements they
  were built from. `HYPOTHESIS_PROFILE=thorough` runs 5000 examples per property.
- `tests/test_ddl_property.py` generates schemas (domains, tables with every data type
  of the server version, defaults, collations, identity and computed columns, PK, UNIQUE,
  CHECK and FK constraints, indices, comments), applies them, extracts their DDL through
  the API, replays it into a second database and requires identical DDL.
  `FBADMIN_DDL_EXAMPLES=200` runs more examples (default 12).

When hypothesis finds a failure it prints the smallest failing example; add it as a
regular test next to the fix.

### firebird-qa compatibility (optional, slow)

`tests/test_firebird_qa.py` reads the isql scripts of the
[firebird-qa](https://github.com/FirebirdSQL/firebird-qa) test suite (parsed with `ast`,
never imported):

- every script must be split by `split_sql()` exactly like isql would (a reference
  splitter implements isql's rules; scripts using Firebird 6 `SET AUTOTERM` are skipped);
- a sample of the DDL-heavy scripts is run into a fresh database, then every object's DDL
  is extracted, replayed into a second database and compared.

```bash
git clone --depth 1 https://github.com/FirebirdSQL/firebird-qa /tmp/firebird-qa
FBADMIN_QA_DIR=/tmp/firebird-qa python -m pytest -q -s tests/test_firebird_qa.py
FBADMIN_QA_CLONE=1 python -m pytest tests/test_firebird_qa.py   # clones into ~/.cache/fbadmin
FBADMIN_QA_SAMPLE=300 FBADMIN_QA_SEED=7 ...                     # bigger sample, other seed
```

It runs weekly in `.github/workflows/compat.yml`. It already found three splitter bugs
(comments before `SET TERM`, `CREATE COLLATION … CASE INSENSITIVE`,
`ALTER DATABASE BEGIN BACKUP`).

### Native driver tests

The native driver (`firebird-driver` + libfbclient, see [DRIVERS.md](DRIVERS.md)) needs a
client library. `firebird-driver` is a regular dependency, but without a client library the
default setting (*Automatic*) uses the pure-Python driver, so a plain checkout runs the suite
with `firebirdsql`. Use the Firebird 5 client from the embedded file set: it is pinned and
checksummed, includes the ChaCha wire-encryption plugin, and unlike a Firebird 3 client
(Ubuntu 24.04's `libfbclient2`) it handles INT128, DECFLOAT and the time zone types of
Firebird 4+ servers.

```bash
scripts/embedded-test-setup.sh                       # → build/firebird-embedded
export FBADMIN_FBCLIENT=$PWD/build/firebird-embedded/lib/libfbclient.so.2
cd backend
python -m pytest -q tests/test_driver_auto.py tests/test_native_driver.py tests/test_driver_equivalence.py
python -m pytest -q                                   # the whole suite; Automatic → native
FBADMIN_TEST_PORT=3053 python -m pytest -q            # ... against Firebird 3
FBADMIN_DRIVER=firebirdsql python -m pytest -q        # the whole suite through the pure-Python driver
```

`FBADMIN_TEST_EXPECT_DRIVER=native` (or `firebirdsql`) makes `test_driver_auto.py` assert what
*Automatic* resolves to on your machine, as CI does.

- `tests/test_driver_auto.py`: the *Automatic* rules and the run-time fallback (a client
  library failure falls back to firebirdsql, a Firebird error does not), ChaCha negotiation.
- `tests/test_native_driver.py`: the driver setting and its API, Query Tool sessions,
  services, wire options, error classes, BLOBs, and switching drivers at run time.
- `tests/test_driver_equivalence.py` runs the same queries and API calls through both drivers
  and requires identical serialized results (INT128, NUMERIC(38), DECFLOAT, time zones,
  BLOBs, UTF8/WIN1252/NONE/OCTETS, parameters, plans, errors, metadata endpoints). When it
  fails it prints the path of every differing value.
- Without a loadable libfbclient these tests skip; with `FBADMIN_DRIVER=native` and no
  library every connection fails with the reason.
- The `backend-native` CI job runs the whole suite with `FBADMIN_DRIVER=native` against
  Firebird 3, 4, 5 and 6; the plain `backend` jobs have no client library, so *Automatic*
  uses firebirdsql there. The `e2e` job runs the Playwright suite with *Automatic* (native
  through the embedded file set's client) and with `FBADMIN_DRIVER=firebirdsql`.

`scripts/driver-compare.py` benchmarks both drivers (see [DRIVERS.md](DRIVERS.md#benchmarks)).

### Local (embedded) database tests

`tests/test_embedded.py` needs no Firebird server. It runs the Firebird 5 embedded engine in
the test process and skips its engine tests unless `FBADMIN_TEST_EMBEDDED_DIR` is set:

```bash
scripts/embedded-test-setup.sh                 # downloads + extracts to build/firebird-embedded
FBADMIN_TEST_EMBEDDED_DIR=$PWD/build/firebird-embedded python -m pytest -q backend/tests/test_embedded.py
```

Start fbadmin with `FBADMIN_EMBEDDED_DIR=$PWD/build/firebird-embedded` to get the **Local
database** button. `e2e/tests/embedded.spec.ts` runs when the server under test has it. See
[EMBEDDED.md](EMBEDDED.md).

## End-to-end tests (Playwright)

`e2e/` is a separate npm package with [Playwright](https://playwright.dev) tests that drive
a real browser against a real fbadmin and a real Firebird server: registering a server,
browsing the tree, properties/DDL/dependencies, running queries, `:name` parameters,
error positions, cancelling a long query, editing and saving data in the grid, and the
opt-in update check (no request before opting in).

```bash
(cd frontend && npm run build)           # the tests use the built UI
cd e2e && npm install
npx playwright install chromium          # once (or use an installed Chromium, see below)
FBADMIN_PYTHON=../.venv/bin/python npx playwright test
npx playwright test --headed             # watch it
npx playwright show-report               # after a failure: traces and screenshots
```

Playwright starts `python -m fbadmin` from `backend/` on port `E2E_PORT` (default 5106)
with a temporary data directory. Variables: `FB_HOST`, `FB_PORT` (3050), `FB_PASSWORD`,
`FB_DB_DIR` (a directory on the Firebird server), `E2E_BASE_URL` (test an already running
instance), `PW_CHROMIUM_PATH` (use a specific Chromium binary). The fbadmin server inherits
the environment, so `FBADMIN_FBCLIENT=…` (or `FBADMIN_EMBEDDED_DIR`) runs the suite through
the native driver (*Automatic*) and `FBADMIN_DRIVER=firebirdsql` through the pure-Python one.
The `e2e` CI job runs them against a Firebird 5 service container, once each way.

## API types

`frontend/src/api-types.gen.ts` holds TypeScript types for every endpoint, generated from
FastAPI's OpenAPI schema with [openapi-typescript](https://openapi-ts.dev):

```bash
scripts/gen-api-types.sh          # regenerate after changing an endpoint or a pydantic model
scripts/gen-api-types.sh --check  # what CI runs: fails when the file is stale
```

Request bodies in the API clients (`frontend/src/api.ts` and the `features/*/api.ts` files) are
typed from it. Use `Body<"/api/schedules", "post">` from `api.ts`, which takes the JSON body of
an OpenAPI path and also fails to compile when the path no longer exists. You can also use the
schemas directly (`components["schemas"]["ServerIn"]`). Request-body fields that have defaults
on the server are optional (`--default-non-nullable false`). Most endpoints return plain
dicts, so their response types are still written by hand in `types.ts`.
After merging branches that touch the API, regenerate the file instead of resolving
conflicts in it by hand.

## Frontend bundle

`vite.config.ts` splits vendor code into long-lived chunks (`react`, `codemirror`,
`sql-formatter`, `vendor`), and `App.tsx` lazy-loads the Query Tool, data grid, monitor
and processes views. The app's own start-up chunk is about 70 kB. CodeMirror is still
loaded at start-up because the SQL tab and dialogs use the editor.

## Packaging

See [Packaging and releases](PACKAGING.md).

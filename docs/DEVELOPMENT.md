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

`tests/test_native_driver.py` runs when `firebird-driver` is installed and libfbclient
can be loaded. To run the whole suite through the native adapter:

```bash
pip install -e "backend[native]"
FBADMIN_DRIVER=native FBADMIN_FBCLIENT=/path/to/libfbclient.so python -m pytest -q
```

(A libfbclient can be copied out of the Firebird container:
`docker cp -L fb5:/opt/firebird/lib/libfbclient.so.2 .`; it also needs `libtommath`.)

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
instance), `PW_CHROMIUM_PATH` (use a specific Chromium binary). The `e2e` CI job runs them
against a Firebird 5 service container.

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

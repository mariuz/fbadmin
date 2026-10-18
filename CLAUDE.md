# fbadmin — notes for contributors (human or AI)

pgAdmin-style web admin for Firebird. Backend: Python/FastAPI in `backend/fbadmin`, pure-Python
`firebirdsql` driver. Frontend: React + TypeScript + Vite in `frontend/src`, built into
`backend/fbadmin/static`.

## Commands
- Tests: `cd backend && ../.venv/bin/python -m pytest -q` (needs a Firebird server; see below)
- Lint: `.venv/bin/ruff check backend`
- Frontend typecheck/build: `cd frontend && npm run build` (run `npm install` first in a fresh checkout)
- Run: `.venv/bin/python -m fbadmin --no-browser --port 5050`

## Test servers (local Docker containers)
FB5 on 127.0.0.1:3050, FB3 on :3053, FB4 on :3054, SYSDBA/masterkey, database dir
`/var/lib/firebird/data`. Select with `FBADMIN_TEST_PORT=3053`. All three must pass.

## Adding features without conflicts
- Frontend: put a feature in `frontend/src/features/<name>/index.tsx` and import it with ONE line
  in `frontend/src/features/index.ts`. Register UI via `frontend/src/registry.tsx`:
  `registerTabKind`, `registerMenu`, `registerTopbar`, `registerObjectTab`, `registerDashboardSection`.
  Open a tab with `app.openTab({ kind: "<your-kind>", title, serverId, dbId, props: {...} })`.
- Backend: add `backend/fbadmin/api/<name>.py` with an `APIRouter` and append it to `ROUTERS`
  in `backend/fbadmin/api/__init__.py` (one line).
- Prefer new files over editing shared ones (App.tsx, menus.tsx, catalog.py, api.ts). Put a
  feature's API client functions in the feature folder, using `request` patterns from `api.ts`.
- Metadata SQL must work on Firebird 3, 4 and 5; detect newer features at runtime.
- Never alias columns with reserved words (POSITION, DETERMINISTIC, ...).

# Contributing to fbadmin

Thanks for helping! Bug reports, roadmap ideas and pull requests are all welcome.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "backend[dev]"
cd frontend && npm install && cd ..
docker run -d --name fb5 -p 3050:3050 -e FIREBIRD_ROOT_PASSWORD=masterkey firebirdsql/firebird:5
```

Run `fbadmin --reload --no-browser` and `npm run dev` (in `frontend/`), then open
http://localhost:5173.

## Before opening a pull request

- `ruff check backend` passes
- `pytest backend/tests` passes against Firebird 5. CI also runs Firebird 3 and 4.
- `npm run build` in `frontend/` passes, including the TypeScript type check
- New metadata or DDL features come with a test. If the object has DDL, add it to
  `backend/tests/sample_schema.sql` so the round-trip test covers it.
- Keep metadata SQL compatible with Firebird 3.0. Detect newer features at runtime
  (see `Catalog.has_column` and `Catalog.engine_major`).

## Style

- Python: type hints, small functions, no ORMs; plain SQL against the system tables.
- TypeScript: function components and hooks, no global state library. Shared state
  lives in `context.tsx`.
- Watch out for Firebird reserved words when you alias columns in metadata queries
  (`POSITION`, `DETERMINISTIC`, …). Use names like `pos` or `is_deterministic`.

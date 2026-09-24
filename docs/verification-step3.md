# Step 3 verification — 2026-09-24

The interrupted working tree was resumed in place. `git status --short` and
`git diff` were attempted, but this directory has no `.git` repository, so no
commit diff or tracked/untracked distinction is available. Files were inspected
directly; the existing implementation was extended rather than replaced.

Verification used Python 3.12 and the existing temporary PostgreSQL 17 instance
on `127.0.0.1:55432`. `DATABASE_URL` and `TEST_DATABASE_URL` were set to that
instance for database checks. Credentials are omitted from this report. Each
integration suite creates and drops its own schema. The temporary server was
stopped after verification; its demo data is not part of the repository.

## Results

- Backend: **102 tests passed**, including the existing tests, deterministic
  scenarios, typed tools, timing/usage, trace order, redaction, source-data
  rejection, API errors, and duplicate execution.
- A real concurrent test with separate database connections verifies the running
  claim, duplicate rejection, and visibility of an earlier committed case error
  while a later provider call is still blocked.
- Empty-schema and populated migration downgrade/reapply tests pass. The populated
  test verifies compatibility mapping of `completed` to legacy `passed`, retaining
  the observable response. Alembic reports no new upgrade operations.
- Ruff lint and formatting pass; strict mypy passes for all **23 source files**.
- Frontend: ESLint passes, **1 test passes**, and TypeScript/Vite build passes.
  No frontend source changes were made.
- Compose configuration validation passes. Container startup was not required
  for verification; the application was tested against real PostgreSQL directly.
- The documented seed commands pass and reuse their project/configuration/dataset.
  API smoke testing without dependency overrides creates and executes a new run,
  retrieves results/traces, verifies **3 completed / 5 error** cases, and checks
  duplicate execution returns 409. Demo run status `failed` is expected.

The backend emits two pre-existing Starlette/httpx/AnyIO deprecation warnings.
They do not cause test failures and were not suppressed.

## Verification commands

From `backend/`, with the two database environment variables set as described.
The cache path below uses `$env:TEMP` instead of the original machine-specific
absolute path; the other commands are unchanged:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic downgrade 0b3e65b38568
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pytest -q -o "cache_dir=$env:TEMP/traceforge-pytest-cache"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m traceforge.demo --execute
.\.venv\Scripts\python.exe -m traceforge.demo
```

The pytest cache override avoids the previously encountered Windows cache-directory
permission conflict; it does not change which tests run. Full downgrade to `base`
and reapply also run inside the PostgreSQL migration integration test.

From `frontend/`:

```powershell
npm run lint
npm test
npm run build
```

From the repository root:

```powershell
docker compose --env-file .env.example config --quiet
```

See [execution.md](execution.md) for the public API smoke flow, trace example,
status compatibility decisions, and remaining limitations.

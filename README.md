# TraceForge

An independently developed evaluation and reliability platform for tool-calling
AI agents. **Current scope: deterministic execution, trace capture, and CRUD APIs.**
Projects, configurations, datasets, cases, and evaluation runs are persisted in
PostgreSQL. Runs execute offline using a deterministic fake provider and typed
fictional support tools. Scoring, real providers, bulk uploads, comparison, replay,
and CI thresholds remain deferred.
The React frontend remains the foundation shell.

See [architecture and milestones](docs/architecture.md) for boundaries, proposed
data-contract decisions, and acceptance criteria. 

## Start with Docker Compose

Prerequisites: Git (optional until initializing the repository), Docker with a
running engine, and Docker Compose v2. No model API keys are required.

From the repository root, copy the local environment template:

```powershell
# PowerShell
Copy-Item .env.example .env
```

```sh
# macOS / Linux
cp .env.example .env
```

Then run:

```sh
docker compose up --build --wait
```

- Frontend: http://localhost:5173
- API health: http://localhost:8000/health (returns `{"status":"ok"}`)
- API documentation: http://localhost:8000/docs
- PostgreSQL: `localhost:5432`, database/user/password `traceforge` by default.

The health link in the UI uses `/api/health`, proxied by Vite to the backend.
The backend waits for PostgreSQL, applies Alembic migrations, and then starts.
`/health` reports process liveness only, independently of database availability.
See [data model and API](docs/data-model.md) for CRUD routes and error semantics.

```sh
docker compose logs -f
docker compose down
```

`down` preserves database data in the named volume. Backend and frontend source
edits reload automatically; rebuild after dependency or configuration changes.
Host ports can be changed in `.env`. These are local development containers with
loopback-only published ports, not production deployment images.

## Start without application containers

Prerequisites: Python **3.12**, Node.js **22.12+** (Node 24 also works), and npm.
PostgreSQL 17 is required for CRUD operations. To provision it alone from the root:

```sh
docker compose up -d db
```

Backend, in a first terminal:

```powershell
# PowerShell, from repository root
cd backend
py -3.12 -m venv .venv
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn traceforge.main:app --reload --host 127.0.0.1 --port 8000
```

```sh
# macOS / Linux, from repository root
cd backend
python3.12 -m venv .venv
cp .env.example .env
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn traceforge.main:app --reload --host 127.0.0.1 --port 8000
```

The backend and Alembic read `DATABASE_URL` from `backend/.env` when invoked from
`backend/`; environment variables take precedence. Change the URL if your local
PostgreSQL port or credentials differ. URL-encode special characters in credentials.
If the Windows Python launcher `py` is unavailable, substitute the full path to an
installed Python 3.12 executable. No model API keys are required.

Frontend, in a second terminal from the repository root:

```sh
cd frontend
npm ci
npm run dev
```

The proxy defaults to `http://127.0.0.1:8000`. To change it, copy
`frontend/.env.example` to `frontend/.env` and edit `API_PROXY_TARGET`.
Restart Vite after changing environment variables. Never put provider secrets in
browser-exposed `VITE_*` variables. The static build requires an `/api` reverse
proxy when eventually deployed; Vite's development proxy is not in the build.

## Checks

Backend (PowerShell, from `backend/`, after installing dependencies):

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
$env:TEST_DATABASE_URL = "postgresql+psycopg://traceforge:traceforge@localhost:5432/traceforge"
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pytest
```

On macOS/Linux, replace `.\.venv\Scripts\python.exe` with `.venv/bin/python` and set
`export TEST_DATABASE_URL=postgresql+psycopg://traceforge:traceforge@localhost:5432/traceforge`.
Run `alembic upgrade head` before `alembic check`. The test URL must be explicitly
exported (pytest does not read it from `.env`); use a local development database
with CREATE SCHEMA permission. Each test suite migrates a unique `traceforge_test_*`
schema, rolls back each test, and drops only that schema afterward. Tests also
exercise downgrade/re-upgrade and check migration/ORM consistency. No SQLite
substitution or `create_all` shortcut is used. To run only database-free tests:
`python -m pytest -m "not integration"` using the virtual environment interpreter.

Frontend (from `frontend/`):

```sh
npm run lint
npm test
npm run build
```

Container equivalents (from the root):

```sh
docker compose --env-file .env.example config --quiet
docker compose build
docker compose run --rm --no-deps backend python -m ruff check .
docker compose run --rm --no-deps backend python -m ruff format --check .
docker compose up -d --wait db
docker compose run --rm backend python -m alembic upgrade head
docker compose run --rm backend python -m alembic check
docker compose run --rm --no-deps backend python -m mypy
docker compose run --rm backend python -m pytest
docker compose run --rm --no-deps frontend npm run lint
docker compose run --rm --no-deps frontend npm test
docker compose run --rm --no-deps frontend npm run build
```

Frontend dependencies are locked in `package-lock.json`. Backend dependencies
currently use bounded ranges in `pyproject.toml`; add a reproducible Python lock
workflow before CI/release. SQLAlchemy 2, Alembic, and psycopg are now installed.
Model-provider SDKs remain deferred. No API key is required.

## Execute the offline demo

See [execution and traces](docs/execution.md) for architecture, lifecycle diagram,
API routes, redaction limits, and a trace example. From `backend/` after migration:

```powershell
.\.venv\Scripts\python.exe -m traceforge.demo --execute
```

Use `.venv/bin/python` on macOS/Linux. The seed is reusable; each execution creates
a new run. Its eight cases intentionally include five recorded errors and three
completed cases, so the demo run ends `failed`. This is expected behavior.

## Next milestone

Add versioned correctness and tool-use scorers writing to `ScoringResult`, without
changing execution status. Define thresholds and failure denominators before CI.
No real provider integration is needed for that milestone.

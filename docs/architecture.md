# MVP architecture and implementation plan

## Architecture

Use one Python 3.12 / FastAPI application, one React / TypeScript / Vite UI, and
PostgreSQL. Keep execution in the same application initially. Avoid microservices,
Kubernetes, authentication, and billing in the MVP. Local ports bind to loopback;
this unauthenticated development stack is not a public deployment configuration.

The foundation now includes HTTP liveness, a UI shell, SQLAlchemy 2 persistence,
Alembic migrations, and CRUD APIs for projects, configurations, datasets, and cases.
See [data model and API](data-model.md) and [execution and traces](execution.md)
for implemented contracts, the flow diagram, and tradeoffs. Provider interfaces,
an offline fake, and typed synthetic support tools now drive run execution.
No model API keys are needed.

Planned dependency direction: API and CLI call application use cases; application
use cases operate on domain records and use provider/persistence adapters. Domain
records and scorers must not depend on FastAPI, SQLAlchemy, or vendor SDKs. Introduce
interfaces at real boundaries, not a generic repository or dependency framework.

```text
TraceForge/
  backend/
    src/traceforge/
      main.py               # current API entry point
      api/                  # CRUD routes and request/response schemas
      domain/               # provider-independent status enums and transition rules
      application/          # runner, evaluation persistence, and trace sanitization
      providers/            # typed provider contract and deterministic fake
      persistence/          # SQLAlchemy records and session management
      tools/                # typed registry and fictional support tools
      demo.py               # idempotent seed and offline demo execution
      cli.py                # future CI entry point
    migrations/             # explicit Alembic revisions
    tests/
    pyproject.toml
    Dockerfile
  frontend/
    src/
    package.json
    package-lock.json
    Dockerfile
  docs/architecture.md
  compose.yaml
  .env.example
  README.md
```

Directories marked future are intentionally not scaffolded yet. PostgreSQL runs
in Compose with a named volume; the backend connects through psycopg.
`GET /health` is liveness, not database readiness. Introduce a separate readiness
check when persistence becomes necessary. Vite proxies `/api/*` to the backend
for local development; production serving and routing are deferred.

## Decisions that are costly to reverse

- **Record identity and immutability:** use UUIDs and immutable agent/dataset
  revisions, referenced by each run. Store effective configuration and scoring
  version with the run so edits cannot change its meaning.
- **Dataset and trace contracts:** version the import format and normalized
  trace schema before exposing them. Preserve tool-call IDs, order, inputs,
  outputs, errors, and raw provider output. Never persist provider credentials.
  Define payload limits and redaction before accepting real customer data.
- **Provider boundary:** keep model/tool messages independent of vendor SDKs.
  Prove the interface using a deterministic fake and contract tests before adding
  OpenAI or Gemini adapters. Do not invent vendor compatibility in advance.
- **Scoring and CI:** distinguish incorrect answers, invalid tool use, and
  infrastructure failures. Specify denominators, empty dataset behavior, threshold
  comparison, and exit codes before users depend on pass/fail results.
- **Replay semantics:** recorded replay displays stored events without executing
  tools; rerun creates a new run and may produce different results. Real external
  side effects are out of scope for the initial fake-tool implementation.
- **Execution durability:** start with bounded synchronous execution and persisted
  statuses. Specify interrupted-run behavior before introducing background runs;
  in-process fire-and-forget work is not a durable queue.
- **Public licensing:** select a license before public release. No license is
  assumed on the owner's behalf.

Some are implemented in the persistence milestone; [data-model.md](data-model.md)
identifies the exact current guarantees and remaining work.

## Small, testable milestones

1. **Foundation (complete):** health route, React shell, PostgreSQL Compose service,
   environment templates, lint/test/build commands, and exact startup instructions.
   Acceptance: API contract test, UI smoke test, Ruff, ESLint, TypeScript/build,
   Compose validation, and a running-stack smoke check where Docker is available.
2. **Configuration and datasets (CRUD/persistence complete; bulk imports deferred):** define versioned JSON import schemas and
   immutable configuration/dataset revisions; implement SQLAlchemy 2 persistence,
   Alembic migrations, and minimal CRUD/import APIs. Acceptance: fresh database
   migration, round-trip imports, rejected invalid rows, and revision isolation.
3. **Deterministic execution (complete; durable recovery deferred):** implement provider messages and fake provider,
   synthetic tools, bounded execution, normalized traces, and run persistence.
   Acceptance: repeated identical inputs produce identical semantic traces;
   malformed calls, tool errors, and timeouts have explicit states. Process loss
   leaves a visible running state and committed partial trace; recovery is deferred.
4. **Scoring:** add exact-match correctness and expected tool-use scoring, versioned
   scorer configuration, and threshold evaluation. Acceptance: known fixtures cover
   pass/fail boundaries, missing answers, infrastructure errors, and empty datasets.
5. **Comparison and replay:** list runs, compare versions on a shared dataset
   revision, inspect case details, and replay recorded failures. Acceptance: replay
   never calls a model or tool; incompatible comparisons are clearly identified.
6. **CI integration:** add a CLI over the same use cases, JSON results, deterministic
   fixtures, and a sample CI workflow. Acceptance: passing, failing, and execution
   error runs return documented distinct exit codes without any API key.

After these milestones, consider real provider adapters, durable execution, and
production deployment based on demonstrated needs.

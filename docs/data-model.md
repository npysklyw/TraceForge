# Domain persistence and CRUD API

The FastAPI API uses SQLAlchemy 2 typed mappings and explicit Alembic revisions
against PostgreSQL 17. The React shell is unchanged. Offline execution and trace
retrieval are implemented; no real provider is called. All tests use original
synthetic examples. See [execution and traces](execution.md) for the flow diagram,
API operations, trace example, and exact demo commands.

## Records and relationships

- **Project:** unique name and description; owns configurations and datasets.
- **AgentConfiguration:** belongs to a project; name, provider identifier (`fake`,
  `openai`, or `gemini`), model name, prompt, and JSONB parameters. These provider
  identifiers describe configuration; only `fake` is executable. Defaults
  select the deterministic fake provider. Do not put credentials in parameters.
- **EvaluationDataset:** belongs to a project; name, description, and format
  `schema_version=1`. This is a format version, not a revision counter.
- **TestCase:** belongs to a dataset; name, JSONB input object, and required JSONB
  expected output. Expected output can be any JSON value, including JSON null.
- **EvaluationRun:** references a project and a configuration/dataset in that
  same project, plus execution status, start/finish times, and an optional error.
- **CaseResult:** references a run and a case in that run's dataset. Contains
  execution status, JSONB response, sanitized input snapshot, provider/model,
  structured error, timestamps, latency, and reported token counts. One result per
  run/case; retries will require an explicit attempts design later.
- **ToolCall:** belongs to a case result; zero-based sequence, optional provider
  call ID, tool name, JSONB arguments/output, and error. Sequence is unique per
  result and determines retrieval order; gaps are allowed. Execution also records
  argument-validation state, timing, latency, and structured errors.
- **ExecutionEvent:** belongs to a case result; unique event sequence, kind,
  sanitized observable JSONB payload, timing, latency, and optional token counts.
  This provides the total order across model and tool observations.
- **ScoringResult:** belongs to a case result; scorer name/version, normalized
  numeric value from 0 to 1, pass/fail flag, and JSONB details. Each scorer version
  can produce one score per case result. Scoring implementation is deferred.

Every record has a UUID primary key, `created_at`, and `updated_at`. PostgreSQL
uses timezone-aware timestamps and sessions use UTC; ORM updates set `updated_at`
in UTC. Direct SQL writers must update `updated_at` themselves. JSON fields are
replaced as whole values when patched; nested dictionaries are not merge-patched
or automatically tracked for in-place ORM mutation.

Foreign keys restrict deletion instead of silently cascading. Project names are
unique globally; configuration/dataset names are unique within their project;
case names are unique within their dataset. Names are trimmed and case-sensitive.
Composite foreign keys enforce project/dataset consistency even outside the API.
Database constraints also enforce JSON object shapes, enum values, score bounds,
sequence bounds, supported providers, and finish-time ordering.

## Status semantics

Runs: `pending`, `running`, `completed`, `failed`, `cancelled`.
`completed` means execution finished, not that every case passed. `failed` means
the run could not execute successfully.

New case execution states: `pending`, `running`, `completed`, `error`, `skipped`.
`completed` means successful execution without a correctness assertion; `error`
means execution failure. The legacy `passed`/`failed` enum values remain readable
for backward compatibility, but new transitions into them are rejected. Future
scoring belongs in `ScoringResult`, not case execution status. A pending case may
be skipped; a pending run may be cancelled without a start timestamp. Terminal
states cannot restart through domain transition rules.

The database validates enum membership, while the domain module validates legal
transitions. The execution service invokes these rules; direct SQL/ORM writes do
not automatically enforce transition history.

## CRUD routes

Backend paths have no `/api` prefix. Vite forwards `/api/*` to these paths.

- `POST /projects`, `GET /projects`
- `GET`, `PATCH`, `DELETE /projects/{project_id}`
- `POST`, `GET /projects/{project_id}/agent-configurations`
- `GET`, `PATCH`, `DELETE /agent-configurations/{agent_id}`
- `POST`, `GET /projects/{project_id}/datasets`
- `GET`, `PATCH`, `DELETE /datasets/{dataset_id}`
- `POST`, `GET /datasets/{dataset_id}/test-cases`
- `GET`, `PATCH`, `DELETE /test-cases/{case_id}`

OpenAPI at `/docs` describes the request/response schemas. Collection responses
contain `items`, `total`, `limit`, and `offset`. Default limit is 20; allowed limits
are 1–100 and offsets are 0–2147483647. Results sort by creation time then UUID.
Offset pagination is intentionally simple: concurrent inserts/deletes can change
pages or totals; it does not promise a snapshot across requests.

Create returns 201, read/update returns 200, and delete returns an empty 204.
Missing records or collection parents return 404. Invalid UUIDs, query parameters,
unknown fields, unsupported values, blank/oversized names, empty patches, and
invalid nulls return 422 with FastAPI validation details. Requests are capped at
256 KiB of serialized validated payload (not a transport-level body limit).
PostgreSQL-incompatible Unicode null characters are rejected before persistence.

Duplicate names and protected deletions return 409 with a useful message, without
leaking SQL or connection information. Each mutation uses a transaction; failures
roll back. `PATCH` modifies only supplied fields; supplying JSON null for
`expected_output` clears it to JSON null, while omitting it preserves the value.
Parent IDs and dataset schema versions cannot be changed with PATCH.

Example using PowerShell after startup:

```powershell
$project = Invoke-RestMethod http://localhost:8000/projects -Method Post -ContentType 'application/json' -Body '{"name":"Synthetic math"}'
$dataset = Invoke-RestMethod "http://localhost:8000/projects/$($project.id)/datasets" -Method Post -ContentType 'application/json' -Body '{"name":"Addition v1"}'
Invoke-RestMethod "http://localhost:8000/datasets/$($dataset.id)/test-cases" -Method Post -ContentType 'application/json' -Body '{"name":"Two plus three","input":{"numbers":[2,3]},"expected_output":5}'
```

## History protection and deliberate limits

Configurations and datasets are mutable drafts until referenced by a run. At that
point, CRUD routes reject their update/deletion and any addition, update, or
deletion of dataset cases with 409. Create a new configuration/dataset with a new
name and UUID to represent the next version. This is smaller than introducing
revision tables and copy endpoints now. There is no immutable historical snapshot
of earlier draft edits, and project display metadata remains editable.

Mutation routes lock records before checking references. Run creation locks the
configuration and dataset before persisting the run and its pending case results.
Direct database writes can bypass API freeze rules; this is not a database audit
or security boundary. Schema-level immutable snapshots/revision lineage remain a
future decision before external writers or richer version comparison are added.

There is no generic repository layer. Shared route helpers cover repeated lookup,
pagination, transaction conflict handling, and field updates only. Application use
cases now live in `application/evaluations.py`, separate from the API transport.

Deferred: real providers, scoring algorithms, bulk imports, run comparison/replay,
full snapshot/revision lineage, retry attempts, durable recovery, authentication,
billing, and production deployment. Current execution preserves a sanitized input
snapshot and freezes referenced source records through the API. The next milestone
is scoring the observable behavior independently of execution success.

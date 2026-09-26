# CLI and pull-request quality gates

Install from `backend/` with `python -m pip install -e '.[dev]'` in a Python 3.12
virtual environment. This installs `benchwarden`; `python -m benchwarden.cli` is
equivalent. Configure `BENCHWARDEN_DATABASE_URL`, then run `python -m alembic upgrade
head`. The CLI accesses PostgreSQL directly through the application services; a
running API server is not required. Dataset, configuration, and run arguments are
UUIDs, not names. The database must already contain the referenced resources.

`run` snapshots and executes through `EvaluationService` and automatically scores
eligible completed cases. `score` uses the same idempotent scoring service as the
API. `compare` is read-only and preserves dataset, expectation, input, and scorer
version compatibility checks. `check` reads those services' persisted metrics and
outcomes; it never changes traces, execution statuses, or scores. No schema changes
or migrations are required for this milestone.

```mermaid
flowchart LR
    CLI[benchwarden CLI] --> Execution[Existing execution service]
    CLI --> Scoring[Existing scoring service]
    CLI --> Comparison[Existing comparison service]
    CLI --> Gate[Read-only gate policy]
    Gate --> Metrics[Existing metrics aggregation]
    Gate --> Comparison
    Execution --> DB[(PostgreSQL snapshots and traces)]
    Scoring --> DB
    Comparison --> DB
```

## Copyable offline example: Bash

From `backend/`, with PostgreSQL running and dependencies installed:

```bash
source .venv/bin/activate
export BENCHWARDEN_DATABASE_URL='postgresql+psycopg://benchwarden:benchwarden@localhost:5432/benchwarden'
python -m alembic upgrade head
set -euo pipefail
reports=$(mktemp -d)
benchwarden seed-ci --json > "$reports/seed.json"
field() { python -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"][sys.argv[2]])' "$1" "$2"; }
dataset=$(field "$reports/seed.json" dataset_id)
baseline_config=$(field "$reports/seed.json" baseline_config_id)
candidate_config=$(field "$reports/seed.json" candidate_config_id)
benchwarden run "$dataset" --config "$baseline_config" --json > "$reports/baseline.json"
benchwarden run "$dataset" --config "$candidate_config" --json > "$reports/candidate.json"
baseline=$(field "$reports/baseline.json" run_id)
candidate=$(field "$reports/candidate.json" run_id)
benchwarden score "$baseline" --json
benchwarden score "$candidate" --json
benchwarden compare "$baseline" "$candidate" --json
benchwarden check "$candidate" --baseline "$baseline" \
  --min-pass-rate 0.90 --min-end-to-end-success-rate 0.85 \
  --min-evaluation-coverage 1 --max-execution-errors 0 \
  --max-token-increase 0.20 --max-cost-increase 0.20 --max-regressions 0
```

The URL above contains fictional local development credentials. Use your own
local database settings, and never place production credentials in workflow files.

## Copyable offline example: PowerShell

From `backend/`:

```powershell
. .\.venv\Scripts\Activate.ps1
$env:BENCHWARDEN_DATABASE_URL = 'postgresql+psycopg://benchwarden:benchwarden@localhost:5432/benchwarden'
python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration failed' }
function Invoke-BenchwardenJson {
    $output = & benchwarden @args --json
    if ($LASTEXITCODE -ne 0) { throw "Benchwarden exited with $LASTEXITCODE" }
    $output | ConvertFrom-Json
}
$seed = (Invoke-BenchwardenJson seed-ci).data
$baseline = (Invoke-BenchwardenJson run $seed.dataset_id --config $seed.baseline_config_id).data.run_id
$candidate = (Invoke-BenchwardenJson run $seed.dataset_id --config $seed.candidate_config_id).data.run_id
Invoke-BenchwardenJson score $baseline
Invoke-BenchwardenJson score $candidate
Invoke-BenchwardenJson compare $baseline $candidate
benchwarden check $candidate --baseline $baseline `
  --min-pass-rate 0.90 --min-end-to-end-success-rate 0.85 `
  --min-evaluation-coverage 1 --max-execution-errors 0 `
  --max-token-increase 0.20 --max-cost-increase 0.20 --max-regressions 0
if ($LASTEXITCODE -ne 0) { throw "Quality gate exited with $LASTEXITCODE" }
```

## Threshold contract

Only explicitly configured checks run; at least one is required. Every boundary
is inclusive: minimum means `>=`; maximum means `<=`. All checks must pass.

- `--min-pass-rate`: passed / scoreable cases (completed execution with expectations).
- `--min-end-to-end-success-rate`: passed / all cases, including execution errors.
- `--min-evaluation-coverage`: scored / all cases.
- `--max-execution-error-rate`: execution errors / all cases.
- These rates are fractions between 0 and 1. `0.90` means 90%; `90` is invalid.
- `--max-execution-errors` and `--max-regressions` are nonnegative integer counts.
  A regressed case is counted once, using the existing comparison classification
  (including a newly failed expectation or newly errored execution).
- `--max-latency-regression`, `--max-token-increase`, and `--max-cost-increase`
  are nonnegative fractional increases relative to an explicit `--baseline`.
  `0.20` permits candidate <= baseline * 1.20; `20` means a 2,000% increase.
  Comparison reports' `percent` fields instead use percentage points of change
  (`20` for a 20% relative increase). Gate `observed` uses fractions (`0.20`).
- Latency uses p95 milliseconds with complete latency coverage in both runs.
  `--max-latency-increase-ms` alternatively limits the absolute candidate-minus-
  baseline p95 difference. Both latency limits may be combined.
- Tokens use complete total-token measurements, not partial known-token sums.
  Cost uses complete estimated USD totals and requires identical pricing snapshots.
  Fictional CI pricing is deliberately supplied; it is not a real provider price.
- Relative/count regression checks require `--baseline`. Supplying a baseline
  always validates compatibility and identical case sets, even for absolute checks.
  Both runs must be finished and every eligible case scored. Empty runs are invalid.
- Missing measurements cause a failed check (`metric_unavailable`), never zero.
  Zero baseline and zero candidate pass a relative check with observed zero;
  positive candidate against zero baseline fails (`zero_baseline`, observed null).
  Improvements have negative deltas and pass nonnegative increase thresholds.
- Thresholds must be finite; increase/absolute latency limits are capped at 1,000,000.
  Measured wall-clock latency varies, even with deterministic fake responses.

## Output and exit codes

Human output is the default. `--json` anywhere emits one JSON object on stdout;
errors also use this contract and do not write a traceback to stderr. `--help`
is the exception: it prints ordinary help and exits successfully.

```json
{"schema_version":1,"command":"check","ok":false,"exit_code":1,"data":{"run_id":"00000000-0000-0000-0000-000000000001","baseline_run_id":null,"passed":false,"metrics":{},"checks":[{"name":"min_pass_rate","passed":false,"observed":"0.5","threshold":"0.9","unit":"rate","reason":"threshold_exceeded"}]},"error":null}
```

The example abbreviates `metrics`; actual responses include the full existing
RunMetrics contract. UUIDs are strings; precise decimal money and check values
are strings; unavailable values are null. Every envelope has `schema_version`,
`command`, `ok`, `exit_code`, `data`, and `error`. Syntax errors may have command null.
Run/score data contains `run_id`, `execution_status`, and `metrics`; comparison data
uses the API's RunComparison schema. Errors contain `type` and sanitized `message`.

- **0:** command succeeded, or all configured gate checks passed. A comparison
  can successfully report regressions; use `check` to enforce policy.
- **1:** quality gate failed, including missing requested measurements.
- **2:** invalid syntax, identifiers, configuration, policy, missing resources,
  unfinished/unscored runs, empty runs, or incompatible comparisons.
- **3:** operational failure (database/migrations/unexpected exception), interruption,
  or a newly executed run with recorded execution errors. The latter still returns
  its run ID and metrics in `data`. Quality failures alone do not fail `run`.

The adapter excludes prompts, configuration secrets, traces, and database URLs from
reports. Database errors and parser errors use fixed messages; known connection
credentials are redacted from report strings. Human errors go to stderr.

## Pull-request gate

`.github/workflows/quality.yml` runs on PRs and main pushes. It starts disposable
PostgreSQL 17, installs dependencies, checks migrations/drift, runs backend and
frontend checks, and executes the Bash workflow above with strict passing thresholds.
Mark the `backend` and `frontend` jobs as required branch-protection checks after
the workflow first runs. A failed CLI check exits 1 and fails the backend job.

`seed-ci` creates a fresh isolated project, two cases, and two configurations per
invocation; it never edits historical experiments or existing demo seeds. Both
configurations use the offline fake provider. `seed-ci --regression` changes only
the candidate's `fake_variant` to `wrong_answer`; final responses then intentionally
violate the exact-output expectation while tool usage and token counts stay fixed.
Tests execute that scenario and **assert** check exits 1, so the normal workflow
stays green. To observe the failure manually, substitute `seed-ci --regression`
in either example; the final check fails with two regressed cases.

No API keys, repository secrets, paid APIs, or external model calls are used.
Dependency/action/container downloads still need network access; evaluation itself
is offline. The CI gate omits wall-clock latency thresholds to avoid timing flakes.
It validates the gate machinery with fixtures, not real-model quality. Real providers,
remote API mode, baseline selection/storage policy, release packaging, and persistent
CI report publishing remain deferred. Python dependencies are bounded but not yet locked.

## Milestone verification

Verified on Python 3.12 with temporary PostgreSQL 17 and the installed console
entry point. From `backend/`, with `BENCHWARDEN_TEST_DATABASE_URL` configured:

```text
python -m pip install --no-deps -e .
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

Results: 221 backend tests passed (54 added for CLI/gates); Ruff clean; 52 Python
files formatted; strict mypy clean across 32 source files. Two existing Starlette
dependency deprecation warnings remain. The full suite includes populated-database
migration tests and passing/failing offline CLI integration tests.

On a separately created disposable database, these commands all passed, followed
by actual `benchwarden seed-ci`, `run`, `score`, `compare`, and `check` subprocesses
for both configurations. Passing gate exited 0; controlled regression exited 1.
The database was removed after verification; existing local demo data was preserved.

```text
python -m alembic upgrade head
python -m alembic downgrade base
python -m alembic upgrade head
python -m alembic check
```

From `frontend/`, `npm run lint`, `npm test` (1 passed), `npx tsc --noEmit`, and
`npm run build` passed. The initial sandboxed esbuild checks could not read ancestor
directories; rerunning with normal filesystem access passed without source changes.
`actionlint -shellcheck='' .github/workflows/quality.yml` passed using checksum-
verified actionlint 1.7.12. Actions are pinned to verified upstream v6 commit IDs.
ShellCheck and an actual hosted GitHub Actions run were not available locally;
the PostgreSQL console smoke independently exercised the workflow's command sequence.

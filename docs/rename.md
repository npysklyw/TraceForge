# Benchwarden rename and existing installations

The product name is **Benchwarden**, the Python distribution and import package
are `benchwarden`, and the frontend package is `benchwarden-frontend`.
The rename itself did not add CLI or CI policies; the subsequent
[CLI milestone](cli-ci.md) provides the `benchwarden` command.

The planned repository URL is <https://github.com/npysklyw/BenchWarden>. The remote
repository has not been renamed. Keep the current origin until this PR merges
and the owner renames the GitHub repository; the metadata URL becomes live then.

## Existing Python and database installations

Reinstall the editable backend after pulling this change. From `backend/`, with
the virtual environment active:

```sh
python -m pip uninstall traceforge-backend
python -m pip install -e '.[dev]'
python -m alembic upgrade head
python -m uvicorn benchwarden.main:app --host 127.0.0.1 --port 8000
```

Change the application connection key in your private configuration to
`BENCHWARDEN_DATABASE_URL` and the integration-test key to
`BENCHWARDEN_TEST_DATABASE_URL`. Keep the existing URL value, database name, user,
and password. The backend still accepts `DATABASE_URL` as a transitional fallback
so an existing installation does not silently connect to a different database.
The prefixed key takes precedence when both environment keys are present. Tests
require the new prefixed key explicitly; they never use the application URL as an
implicit target. The frontend proxy key is `BENCHWARDEN_API_PROXY_TARGET`.

No table, column, enum, revision identifier, revision ordering, stored evaluation,
or scoring rule changes. All three existing migrations remain byte-for-byte
unchanged; only the Alembic environment imports the new package. No new migration
is needed. Existing databases remain at revision `2c2af13af5e5`; fresh databases
still execute the entire original migration chain.

## Existing Compose installations

Fresh installations use Compose project `benchwarden`, generated containers,
images and network under that project, and volume `benchwarden_postgres_data`.
Service keys stay `db`, `backend`, and `frontend`. `postgres:17-alpine` and its
container-side `POSTGRES_*` keys are the upstream image contract and stay unchanged.

Before changing the root `.env`, stop the previous Compose project without
deleting its volume:

```sh
docker compose -p traceforge down
docker volume inspect traceforge_postgres_data
```

If the old installation used a custom project/volume name, substitute that actual
name. Never run `down -v` for a database you intend to keep. Back up the database
before changing deployment configuration.

In the root `.env`, carry forward the existing values under these names:

```dotenv
BENCHWARDEN_POSTGRES_USER=traceforge
BENCHWARDEN_POSTGRES_PASSWORD=traceforge
BENCHWARDEN_POSTGRES_DB=traceforge
BENCHWARDEN_POSTGRES_VOLUME=traceforge_postgres_data
BENCHWARDEN_POSTGRES_PORT=5432
BENCHWARDEN_BACKEND_PORT=8000
BENCHWARDEN_FRONTEND_PORT=5173
```

These credentials are the old fictional local defaults, not production values.
Use your existing private values. Reusing the old volume with new credentials
does not rename an existing role/database: PostgreSQL initialization variables
only apply to an empty volume. Do not overwrite your private `.env` with the new
template during this upgrade. The explicit volume setting attaches the existing
data volume to the newly named project instead of creating an empty database.

```sh
docker compose up --build --wait
```

## Intentional old-name references

- `backend/src/benchwarden/demo.py` retains the two exact stored names
  `TraceForge fictional support demo` and
  `TraceForge fictional support scoring demo` for lookup only. Seeding reuses
  their existing IDs and does not rewrite names, cases, runs, traces, or scores.
  Fresh seeds use the new product name. There is no old Python import alias.
- `backend/tests/test_branding.py` uses those same old names to verify populated
  installation compatibility and unchanged stored evaluation data.
- This guide retains the old distribution, Compose project, volume, and default
  database credentials solely to provide concrete existing-installation commands.

The local checkout directory, ignored virtual environments/caches, `.git` history,
and origin URL are outside tracked product content. They may retain the previous
name. They are not renamed or committed by this change. Historical Step 3 command
examples have been updated to the current package/configuration names; their
recorded verification results still describe that original milestone.

## Pull request and later repository rename

Open a PR from `chore/rename-benchwarden` into `main` on the current origin, with
the upgrade instructions above in its description. Do not change the GitHub
repository name before review/merge. After the PR merges, the owner can rename
the repository to `BenchWarden` in its repository settings and update local clones:

```sh
git remote set-url origin https://github.com/npysklyw/BenchWarden.git
git fetch origin
```

No Git history rewrite is necessary.

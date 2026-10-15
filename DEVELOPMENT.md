# Local development

This guide covers the shortest supported setup path, everyday contributor
commands, and common local failures. The helper uses only the Python standard
library and runs the same underlying `uv`, Docker Compose, Alembic, Ruff, and
Pytest commands documented by the project.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker with the Compose v2 plugin, or reachable PostgreSQL 17 and Redis services

Check the tools before setup:

```bash
python --version
uv --version
docker compose version
```

## One-command setup

From the repository root, run:

```bash
python scripts/dev.py setup
```

The command performs these idempotent steps:

1. Copies `.env.example` to `.env` when needed and generates a local
   `SECRET_KEY`. An existing `.env` is never overwritten.
2. Installs the exact locked development dependencies.
3. Starts PostgreSQL and Redis and waits for their health checks.

Setup deliberately does not apply migrations. Review each pending migration
and take a database backup before applying it. For an existing or valuable
database, follow your normal backup and restore verification procedure first.
Then apply changes explicitly with `python scripts/dev.py migrate`.

To use PostgreSQL and Redis outside Docker, set `DATABASE_URL` and `REDIS_URL`
in `.env` first and run:

```bash
python scripts/dev.py setup --skip-docker
```

## Run the API

```bash
python scripts/dev.py serve
```

Open <http://127.0.0.1:8000/docs>. To create a local user, call `POST
/register/` from Swagger UI; the repository intentionally does not ship a
shared demo password.

To exercise the production image locally, first review the migration files and
back up any existing database, then apply migrations and start the stack:

```bash
docker compose --profile migration run --rm migrate
python scripts/dev.py stack-up
```

Compose binds PostgreSQL, Redis, and the API ports to loopback only. The
`migrate` service uses the same locked production image, but its migration
profile prevents ordinary `docker compose up` from applying schema changes.
The API container runs without root privileges, Linux capabilities, or a
writable root filesystem. Inspect it with `docker compose ps` and
`docker compose logs api`, then stop it without deleting database data using
`docker compose down`.

The full stack also starts the Celery `worker` service. Follow its logs with
`docker compose logs -f worker`; the worker health check uses Celery's worker
ping, while `/health/jobs` reports database and broker availability, a worker
ping, and persisted dispatcher and worker-heartbeat freshness.
The API readiness check does not depend on a running worker. If you run the API
on the host, start a worker in another terminal after `python scripts/dev.py
setup`:

```bash
uv run celery -A app.workers.celery_app:celery_app worker -l INFO -Q jobs
```

Run reviewed schema changes with `python scripts/dev.py migrate`. The API and
worker read the same `DATABASE_URL` and `CELERY_BROKER_URL`; the Compose stack
sets container-local URLs while `.env.example` provides local host URLs. The
setup helper generates a local PostgreSQL password when it creates `.env`.

Job results and lifecycle history live in PostgreSQL, not Celery's result
backend. Register and log in through Swagger, submit one of `simulation.delay`,
`data.process`, or `system.health`, and inspect the job and `/history` endpoint.
Automatic retries are bounded by `JOB_MAX_RETRIES` and use capped exponential
backoff. Permanent failures cannot be retried; manual retries of eligible jobs
consume the same retry budget. A queued job can be cancelled; running work
cannot be cancelled safely. Broker rejection produces
HTTP 503 and a persisted failed state. Unit job tests use isolated SQLite
databases and do not require Redis. Do not run database integration tests
unless `DATABASE_URL` points to a disposable test database; existing repository
fixtures may write test records.

## Run durable email workers

Generate a dedicated Fernet key, set `EMAIL_DELIVERY_MODE=outbox`, configure
SMTP, apply migrations, and start a worker in a second terminal:

```bash
uv run fastapi-production-worker
```

Start additional identical processes to test horizontal claims. For one
deterministic batch without polling, use:

```bash
uv run fastapi-production-worker --once
```

Workers stop claiming on `SIGTERM`/`SIGINT`. They finish in-flight work within
the configured grace period; abandoned leases become claimable after expiry.
Never run workers against a database while its `OUTBOX_ENCRYPTION_KEY` is
missing or different from the key used to enqueue pending payloads.

## Everyday commands

| Command | Purpose |
| --- | --- |
| `python scripts/dev.py db-up` | Start PostgreSQL and Redis and wait until healthy |
| `python scripts/dev.py stack-up` | Build and start the complete containerized stack |
| `python scripts/dev.py db-down` | Stop Compose services without deleting data |
| `python scripts/dev.py migrate` | Apply pending Alembic migrations |
| `python scripts/dev.py serve` | Run Uvicorn with auto-reload |
| `python scripts/dev.py check` | Run lint, format check, migrations, tests, audit, and build |

The helper is a convenience layer. Individual commands remain available for
focused work, for example `uv run pytest tests/test_login.py` or `uv run ruff
format .`.

To run the real Redis concurrency and TTL tests against the local Compose
service in PowerShell:

```powershell
$env:REDIS_TEST_URL='redis://localhost:6379/15'; uv run pytest tests/test_redis_rate_limit_integration.py
```

The test database is flushed before and after these tests. Never point
`REDIS_TEST_URL` at a shared or production Redis database.

Worker concurrency tests require PostgreSQL because SQLite does not implement
`FOR UPDATE SKIP LOCKED`. The standard CI job runs these tests on PostgreSQL.

## Database performance evaluation

The opt-in benchmark compares the maintained synchronous engine with a separate
asyncpg prototype without changing application code. It requires an explicitly
named local/test database and owns only the `fastapi_benchmark` schema. Run the
short smoke workflow or the controlled extended matrix in
[DATABASE_BENCHMARKS.md](DATABASE_BENCHMARKS.md). Never use production
credentials or infer performance regressions from CI runner timings.

The benchmark integration test reads `BENCHMARK_DATABASE_URL` and never falls
back to the application's `DATABASE_URL`. Provision a dedicated, empty local
PostgreSQL database such as `fastapi_bench_db`, verify its host and contents,
then set the variable for the test process. The fixture truncates its tables and
the test drops the entire `fastapi_benchmark` schema during cleanup, so never
target a database that contains useful objects in that schema:

```powershell
$env:BENCHMARK_DATABASE_URL = "postgresql+psycopg://<user>:<password>@localhost:5432/fastapi_bench_db"
uv run pytest tests/test_database_benchmark.py
```

The URL guard rejects non-PostgreSQL, non-local, and names without `test`,
`bench`, `local`, or `dev`. The test owns only the `fastapi_benchmark` schema.
Without a dedicated URL, the database-writing benchmark test is skipped; its
safety guard remains enabled.

## Redis integration tests

Set `REDIS_TEST_URL` only to a dedicated test Redis instance. These integration
tests call `FLUSHDB` on the selected Redis database, so do not use the
application's Redis service or any shared, development, or production instance.
For example, start a separate local Redis container on port 6380, then run:

```powershell
$env:REDIS_TEST_URL = "redis://127.0.0.1:6380/0"
uv run pytest tests/test_redis_rate_limit_integration.py tests/test_oidc_cache_integration.py
```

If `REDIS_TEST_URL` is unset, the integration tests are skipped and must not be
reported as passed.

## Windows environment overrides and uv cache

Process environment variables take precedence over `.env`. If a shell or IDE
sets `DEBUG` to a non-boolean value, remove that override or use `true` or
`false`; the development `.env` value can then be parsed normally. Do not set
`DEBUG` in application code to work around a shell configuration issue.

If uv cannot write its default cache, point it at a user-writable temp folder
for the command:

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP "fastapi-production-api-uv-cache"
python scripts/dev.py check
```

## Typical contribution workflow

```bash
git switch main
git pull --ff-only
git switch -c fix/short-description
python scripts/dev.py setup
python scripts/dev.py check
```

Commit only source files and intentional lock-file changes. `.env`, databases,
coverage output, and distributions are ignored by Git.

## Troubleshooting

### Port 5432 or 6379 is already in use

Stop the conflicting service, or point `DATABASE_URL` and `REDIS_URL` at
reachable dependencies and use `setup --skip-docker`. Do not run duplicate
services on the same host ports.

### PostgreSQL or Redis does not become healthy

Inspect the containers with `docker compose ps`, `docker compose logs postgres`,
and `docker compose logs redis`. Confirm Docker has enough disk space and that
Compose values match the local dependency URLs.

### Docker reports an API 500 or cannot reach the Linux engine

Start or restart Docker Desktop, confirm it is using Linux containers, and run
`docker info`. The setup helper stops early with a focused message until that
command succeeds; no project data is changed by this preflight check.

### Migrations cannot connect

Run `python scripts/dev.py db-up`, then check `docker compose ps`. If you use an
external database, verify its hostname, port, database name, and credentials in
`.env`.

Environment variables in the current shell take precedence over `.env`. If
Alembic reports an unexpected database backend, inspect `DATABASE_URL` with
`echo $DATABASE_URL` on Unix or `$env:DATABASE_URL` in PowerShell, clear the
stale value, and open a new terminal if necessary.

### Recreate the local database

`docker compose down --volumes` permanently deletes the local Compose database
volume. Use it only when the data is disposable, then rerun `python
scripts/dev.py setup`.

### The quality gate changes files or fails

Apply formatting with `uv run ruff format .`, rerun the focused failing test,
and then run `python scripts/dev.py check` again. The coverage gate requires at
least 90% combined statement-and-branch coverage.

## OpenTelemetry tracing

Tracing is optional and disabled by default. Existing local development
behavior is unchanged while `TRACING_ENABLED=false`.

To send traces to a local OTLP/HTTP-compatible Collector, configure:

    TRACING_ENABLED=true
    OTEL_SERVICE_NAME=fastapi-production-api
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    OTEL_EXPORT_TIMEOUT_SECONDS=5
    OTEL_TRACE_SAMPLE_RATIO=1.0

Start the Collector separately, then restart the API so tracing is initialized
for that process.

The normal test suite does not require an external telemetry backend. Tracing
tests use in-memory or mocked exporters.

Focused tracing tests:

    uv run pytest tests/test_tracing.py tests/test_logging.py tests/test_outbox_tracing.py

Tracing must not capture request or response bodies, authorization headers,
cookies, credentials, lifecycle tokens, raw email addresses, OIDC
authorization codes, state, nonce, PKCE values, or sensitive query strings.

Transactional outbox propagation stores only bounded W3C `traceparent` and
`tracestate`. Do not persist W3C baggage or application payload data as trace
metadata.

## OIDC discovery and JWKS cache

OIDC public-document caching is optional and disabled by default. Keeping
`OIDC_CACHE_BACKEND=none` preserves the direct-provider behavior.

For local Redis-backed caching, configure the normal Redis connection and set:

```env
OIDC_CACHE_BACKEND=redis
OIDC_DISCOVERY_CACHE_TTL_SECONDS=300
OIDC_JWKS_CACHE_TTL_SECONDS=300
OIDC_CACHE_REFRESH_LOCK_SECONDS=5
OIDC_CACHE_REFRESH_WAIT_SECONDS=1
```

The cache stores only public OpenID Connect discovery documents and JWKS
documents. Tokens, authorization decisions, claims, sessions, user data, and
other authentication state must never be cached by this subsystem.

Every document read from Redis is validated again before it can influence OIDC
processing. Invalid, malformed, or oversized cached values are rejected and
discarded. Cached discovery metadata must still match the configured issuer,
and cached JWKS must still pass the normal key-set validation.

Redis is an optimization rather than an authentication dependency. If Redis is
unavailable, the application falls through to the OIDC provider directly. If
both Redis and the provider are unavailable, the request fails normally rather
than trusting expired cache data.

When a token references a `kid` that is not present in a valid cached JWKS, the
provider JWKS is refreshed once before the token is rejected. Normal issuer,
algorithm, signature, audience, and claim validation remain unchanged.

To invalidate the discovery and JWKS entries for only the configured issuer:

```bash
uv run fastapi-production-cache invalidate-oidc
```

The command does not scan or flush Redis globally. Cache keys are versioned,
bounded, and derived from a digest of the issuer rather than storing the raw
issuer in the key.

Redis-backed integration tests require `REDIS_TEST_URL`. CI supplies a
dedicated Redis database so multi-instance sharing, TTL behavior, invalidation,
outage fallback, and refresh-lock behavior can be exercised without touching
unrelated Redis data.

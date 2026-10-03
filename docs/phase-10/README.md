# Phase 10 - Testing, Monitoring And Security

## Implemented

- Operational middleware:
  - request logging with method, path, status and duration.
  - security headers:
    - `X-Content-Type-Options: nosniff`
    - `X-Frame-Options: DENY`
    - `Referrer-Policy: strict-origin-when-cross-origin`
    - `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- Basic in-memory rate limiting:
  - auth register/login.
  - prediction endpoint.
- Admin monitoring API:
  - `GET /api/v1/admin/system/summary`
  - totals for users, regions, datasets, training runs, model versions and
    predictions.
  - status breakdowns for datasets, training runs and model versions.
  - dependency status for database, MinIO and MLflow.
  - recent training runs and recent predictions.
- End-to-end smoke test script:
  - liveness and readiness.
  - admin login.
  - system summary.
  - public region list.
  - prediction if an active model exists.
- Phase 10 tests for:
  - rate limiting.
  - security headers.
  - monitoring status counters.

## Run Backend Tests

```powershell
docker compose run --rm --no-deps traffic-api python -m unittest `
  tests.test_phase10_operations `
  tests.test_prediction_service `
  tests.test_model_registry_service `
  tests.test_region_pipeline `
  tests.test_auth_service `
  tests.test_region_service `
  tests.test_dataset_validation `
  tests.test_inference
```

## Run Frontend Build Check

```powershell
docker compose run --rm --no-deps traffic-web npm run build
```

## Run Smoke Test

Start the stack:

```powershell
docker compose up -d
```

Run smoke test from the API container:

```powershell
docker compose run --rm --no-deps traffic-api python scripts/ops/smoke_test.py
```

Optional environment variables:

```powershell
$env:SMOKE_API_BASE_URL="http://localhost:8000/api/v1"
$env:SMOKE_ADMIN_EMAIL="admin@example.com"
$env:SMOKE_ADMIN_PASSWORD="admin123456"
```

The script defaults to `http://traffic-api:8000/api/v1` because the recommended
execution path is inside Docker Compose. Use `SMOKE_API_BASE_URL` when running
from the host machine.

## Monitoring API

Login as admin, then call:

```powershell
Invoke-RestMethod `
  -Uri "http://localhost:8000/api/v1/admin/system/summary" `
  -Headers $headers
```

Expected response shape:

```json
{
  "status": "healthy",
  "totals": {
    "users": 2,
    "regions": 1,
    "datasets": 1,
    "training_runs": 4,
    "model_versions": 2,
    "predictions": 3
  },
  "datasets_by_status": {},
  "training_runs_by_status": {},
  "model_versions_by_status": {},
  "dependencies": {
    "database": { "status": "healthy" },
    "minio": { "status": "healthy" },
    "mlflow": { "status": "healthy" }
  }
}
```

## Security Checklist

- Replace all local defaults in `.env` before production:
  - `AUTH_SECRET_KEY`
  - `POSTGRES_PASSWORD`
  - `MINIO_ROOT_PASSWORD`
  - `AIRFLOW_SECRET_KEY`
  - `AIRFLOW_ADMIN_PASSWORD`
  - `INTERNAL_TRAINING_TOKEN`
- Keep `.env` out of git.
- Restrict `CORS_ORIGINS` to the production web domain.
- Keep upload limits enabled:
  - `DATASET_MAX_UPLOAD_BYTES`
  - `DATASET_MIN_ROWS`
- Do not log passwords, tokens or raw uploaded dataset contents.
- Keep admin-only APIs protected by JWT role checks.
- Keep Airflow training execution behind the internal token endpoint.

## Residual Limitations

- Rate limiting is in-memory and per API process. For multi-replica Kubernetes,
  replace it with Redis or API gateway rate limiting.
- This phase adds operational summaries, not full Prometheus metrics. Prometheus
  scraping can be added after Kubernetes manifests are in place.

# Phase 6 - MLflow Tracking and Model Registry

## Implemented

- Pipeline returns MLflow run/model URI metadata when MLflow is installed.
- Region training CLI registers completed training output into PostgreSQL.
- `training_runs` records store split policy and training configuration.
- `model_versions` records store model variant, metrics, MLflow URI, and artifact URI.
- Admin APIs list, inspect, and activate model versions by region.

## API endpoints

```text
GET  /api/v1/admin/regions/{region_id}/model-versions
GET  /api/v1/admin/model-versions/{model_version_id}
POST /api/v1/admin/model-versions/{model_version_id}/activate
```

Activation updates:

- `model_versions.status = active`
- previous active model for the region becomes `archived`
- `regions.active_model_version_id` points at the selected model

## Training Registration

After `scripts/training/run_region_pipeline.py` finishes training, it writes:

- one completed `training_runs` row
- one candidate `model_versions` row

The script prints both IDs:

```text
training_run_id
model_version_id
model_version_status
```

Existing local model info JSON files can also be imported:

```powershell
docker compose run --rm traffic-api python scripts/training/register_model_info.py `
  --region-id "<region_id>" `
  --dataset-id "<dataset_id>" `
  --model-info-path "models/regions/<region_id>/models/candidate_model_info.json"
```

## PowerShell Smoke Test

```powershell
$login = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/auth/login" `
  -ContentType "application/json" `
  -Body '{"email":"admin@example.com","password":"admin123456"}'

$headers = @{ Authorization = "Bearer $($login.access_token)" }

$models = Invoke-RestMethod `
  -Uri "http://localhost:8000/api/v1/admin/regions/<region_id>/model-versions" `
  -Headers $headers

$modelId = $models.items[0].id

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/admin/model-versions/$modelId/activate" `
  -Headers $headers
```

## Current Boundary

Phase 6 registers model metadata and supports manual activation. Phase 7 will
move training execution into Airflow and make `training_runs` lifecycle updates
asynchronous.

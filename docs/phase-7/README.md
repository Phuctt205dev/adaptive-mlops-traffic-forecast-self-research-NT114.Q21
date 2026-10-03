# Phase 7 - Airflow Training Pipeline

## Implemented

- Official Airflow image used only for orchestration.
- Training executes inside the `traffic-api` container through an internal API
  endpoint, so Airflow does not need ML dependencies.
- Airflow DAG `traffic_region_training`.
- Admin API endpoint to trigger training for a dataset.
- API creates a queued `training_runs` row before triggering Airflow.
- Airflow passes `training_run_id` to the internal API execution endpoint.
- The internal API updates the same row to `completed` and creates a candidate
  `model_versions` row after the region pipeline finishes.

## DAG

```text
traffic_region_training
```

Required DAG conf:

```json
{
  "region_id": "<uuid>",
  "dataset_id": "<uuid>",
  "training_run_id": "<uuid>",
  "train_start_date": "2012-10-02",
  "train_end_date": "2013-10-02"
}
```

Optional DAG conf:

```json
{
  "artifact_root": "models/regions",
  "model_role": "candidate",
  "cv_splits": 3,
  "random_state": 42
}
```

## API Trigger

```text
POST /api/v1/admin/datasets/{dataset_id}/training-runs
```

Example:

```powershell
$body = @{
  train_start_date = "2012-10-02"
  train_end_date = "2013-10-02"
  artifact_root = "models/regions"
  model_role = "candidate"
  cv_splits = 3
  random_state = 42
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/admin/datasets/<dataset_id>/training-runs" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

## Airflow UI

```text
http://localhost:8080
username: admin
password: admin
```

The DAG is paused by default because Airflow is configured with
`AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=true`. Unpause
`traffic_region_training` in the UI or CLI before triggering from the API.

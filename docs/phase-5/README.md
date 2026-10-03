# Phase 5 - Refactor ML Pipeline by Region

## Implemented

- `src.pipeline.run_pipeline` can now train from an explicit `data_path`.
- Training metadata accepts `region_id` and `dataset_id`.
- Model artifacts and data versions can be scoped per region.
- Existing calls remain compatible with the old default dataset path.
- Drift worker passes `DATA_PATH`, `REGION_ID`, `DATASET_ID`, and
  `ARTIFACT_ROOT` into the pipeline.
- Added `scripts/training/run_region_pipeline.py` as the bridge for the later
  Airflow phase.

## Artifact layout

When `region_id` is provided, default artifacts are written under:

```text
<artifact_root>/regions/<region_id>/
  data_versions/
    data_v1.csv
    version_log.csv
  models/
    best_model.pkl
    best_model_info.json
    model_v1.pkl
    model_versions.csv
```

The model info JSON now includes:

```json
{
  "region_id": "...",
  "dataset_id": "...",
  "data_path": "...",
  "data_version": "data_v1"
}
```

## Region dataset training script

The script expects a valid dataset row from PostgreSQL. It downloads the raw
CSV from MinIO unless `--local-data-path` is provided.

```powershell
docker compose run --rm traffic-api python scripts/training/run_region_pipeline.py `
  --region-id "<region_id>" `
  --dataset-id "<dataset_id>" `
  --train-start-date "2012-10-02" `
  --train-end-date "2013-10-02" `
  --artifact-root "models/regions" `
  --model-role "candidate"
```

This phase does not yet create `training_runs` or `model_versions` database
records. That belongs to Phase 6 and Phase 7, where MLflow registry and Airflow
orchestration become the source of truth.

# Training Pipeline Contract

## Trigger request

```json
{
  "training_run_id": "uuid",
  "region_id": "uuid",
  "dataset_id": "uuid",
  "raw_dataset_uri": "s3://traffic-datasets/regions/.../original.csv",
  "dataset_sha256": "hex-string",
  "dvc_rev": "string",
  "variants": [
    "random_forest_no_lag",
    "random_forest_lag",
    "xgboost_no_lag",
    "xgboost_lag",
    "lightgbm_no_lag",
    "lightgbm_lag",
    "lstm",
    "gru"
  ],
  "split_policy": "chronological-70-15-15/v1",
  "cv_splits": 5,
  "sequence_length": 168,
  "max_epochs": 20,
  "batch_size": 128,
  "random_state": 42
}
```

The API creates the training-run record before triggering Airflow. Airflow
receives identifiers and immutable object URIs, not uploaded file bytes.

## Pipeline stages

```text
validate_dataset
  -> prepare_hourly_data
  -> create_features
  -> split_70_15_15
  -> train_variants
  -> compare_variants
  -> register_model_versions
  -> recommend_model
  -> complete_training_run
```

## Output contract

```json
{
  "training_run_id": "uuid",
  "status": "completed",
  "recommended_model_version_id": "uuid",
  "models": [
    {
      "model_version_id": "uuid",
      "variant": "lightgbm_lag",
      "mlflow_run_id": "string",
      "cv_mean_mae": 310.5,
      "cv_std_mae": 12.4,
      "final_test_mae": 325.2,
      "artifact_uri": "s3://traffic-models/regions/..."
    }
  ]
}
```

## Required lineage

Every MLflow run and model-version record includes:

- `region_id`, `dataset_id`, and `training_run_id`.
- Dataset SHA-256 and DVC revision.
- Git commit and contract versions.
- Split ranges and fold ranges.
- Hyperparameters, metrics, and artifact URIs.
- Whether Production was used; this value must be `false`.

## Selection and activation

- Variants are ranked by CV mean MAE.
- CV MAE standard deviation and artifact size are tie-breakers.
- Final Test MAE is reporting-only.
- The top-ranked model becomes the recommended candidate.
- Recommendation does not automatically activate the model.
- Admin activation is transactional and archived models may be reactivated.
- Failed or partial runs never change the active model.

## Isolation and idempotency

- Outputs are scoped by region and training run.
- Re-running the same Airflow task must not create conflicting model versions.
- Concurrent runs must not write to shared model or report paths.
- A run can be cancelled, but already written immutable artifacts remain for
  audit and cleanup policies.

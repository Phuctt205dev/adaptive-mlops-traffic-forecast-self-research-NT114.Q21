# Database Schema

PostgreSQL is the application metadata store. Primary keys are UUIDs and all
tables include `created_at` and `updated_at` timestamps.

## Core tables

### `users`

```text
id, email, password_hash, role, is_active, last_login_at
```

- Unique: normalized `email`.
- Role: `admin` or `user`.

### `regions`

```text
id, name, description, latitude, longitude, timezone,
is_active, active_model_version_id
```

- A region represents one independent traffic-volume series.
- `active_model_version_id` is nullable and references a model from the same
  region.

### `datasets`

```text
id, region_id, contract_version, original_filename, storage_uri,
sha256, dvc_rev, row_count, start_at, end_at, status,
quality_report_uri, validation_error, uploaded_by
```

- Unique: `(region_id, sha256)`.
- Dataset objects are immutable.

### `training_runs`

```text
id, region_id, dataset_id, airflow_dag_run_id, status,
split_policy, configuration_json, recommended_model_version_id,
error_message, started_at, completed_at, requested_by
```

### `model_versions`

```text
id, region_id, training_run_id, variant, version,
mlflow_run_id, mlflow_model_uri, artifact_uri,
cv_mean_mae, cv_std_mae, final_test_mae, status
```

- Unique: `(region_id, version)`.
- Only one model may be `active` per region.

### `predictions`

```text
id, user_id, region_id, model_version_id, forecast_for,
forecast_mode, prediction, feature_snapshot_json, created_at
```

### `audit_logs`

```text
id, actor_user_id, action, entity_type, entity_id,
before_json, after_json, created_at
```

## Relationships

```text
users    1 ---- * datasets
users    1 ---- * predictions
regions  1 ---- * datasets
regions  1 ---- * training_runs
regions  1 ---- * model_versions
regions  1 ---- * predictions
datasets 1 ---- * training_runs
training_runs 1 ---- * model_versions
```

## Integrity rules

- Deleting users, regions, datasets, training runs, or model versions is not a
  normal API operation. Records are deactivated or archived.
- A training run's dataset must belong to the same region.
- A model version must belong to its training run's region.
- Activating a model locks the region row, archives the previous active model,
  activates the selected model, and updates `active_model_version_id` in one
  transaction.
- JSON fields are used for flexible configuration and snapshots, not for core
  relationships that require foreign keys.

## Initial indexes

```text
users(email)
datasets(region_id, created_at)
datasets(region_id, status)
training_runs(region_id, created_at)
training_runs(status)
model_versions(region_id, status)
predictions(user_id, created_at)
predictions(region_id, created_at)
```

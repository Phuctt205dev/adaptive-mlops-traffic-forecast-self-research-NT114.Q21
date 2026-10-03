# Target Architecture

## Components

```text
React + TypeScript
        |
        v
FastAPI management and prediction API
        |
        +-- PostgreSQL: users and application metadata
        +-- MinIO: raw/processed datasets, reports, and model artifacts
        +-- Airflow: validation, preprocessing, training, and evaluation
        +-- MLflow: experiment tracking and model registry
        +-- DVC: dataset version references backed by MinIO
```

Docker Compose is the development and demonstration environment. Kubernetes
is introduced only after the complete workflow is stable.

## Ownership boundaries

- FastAPI owns authentication, authorization, metadata CRUD, upload requests,
  prediction requests, and Airflow triggers.
- PostgreSQL is the source of truth for application state and active model
  selection.
- MinIO stores immutable large objects. PostgreSQL stores their URIs and
  checksums, not their bytes.
- Airflow owns long-running and retryable workflow execution.
- MLflow owns run metrics, parameters, artifacts, and registered model
  versions.
- DVC records reproducible dataset versions. It does not replace application
  dataset metadata.

## Storage layout

```text
traffic-datasets/
  regions/{region_id}/datasets/{dataset_id}/raw/original.csv
  regions/{region_id}/datasets/{dataset_id}/processed/hourly.csv
  regions/{region_id}/datasets/{dataset_id}/processed/features.csv
  regions/{region_id}/datasets/{dataset_id}/reports/quality.json

traffic-models/
  regions/{region_id}/training-runs/{training_run_id}/{variant}/

mlflow-artifacts/
  ...

dvc-storage/
  ...
```

Objects are immutable. A corrected upload creates a new dataset ID.

## Security boundaries

- Public access: registration, login, health endpoints.
- Authenticated user access: list active regions, predict, own prediction
  history.
- Admin access: user, region, dataset, training-run, and model management.
- Airflow, MLflow, MinIO, and PostgreSQL are not exposed publicly.
- Secrets are supplied through environment variables in development and
  Kubernetes Secrets in deployment.

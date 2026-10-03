# Phase 1 - Local Infrastructure

Phase 1 provides the shared services required by the management application
and MLOps workflow.

## Services

| Service | Local URL | Purpose |
| --- | --- | --- |
| Traffic API | `http://localhost:8000` | Existing API |
| PostgreSQL | `localhost:5432` | Application, MLflow, and Airflow metadata |
| MinIO API | `http://localhost:9000` | S3-compatible object storage |
| MinIO console | `http://localhost:9001` | Object-storage administration |
| MLflow | `http://localhost:5000` | Tracking server and model registry |
| Airflow | `http://localhost:8080` | Workflow orchestration |

PostgreSQL contains three separate databases:

```text
traffic_management
mlflow
airflow
```

MinIO initialization creates:

```text
traffic-datasets
traffic-models
mlflow-artifacts
dvc-storage
```

## Start

Docker Desktop must be running.

```powershell
Copy-Item .env.example .env
docker compose config
docker compose up --build -d
docker compose ps
```

For normal development and management-API updates, do not add `--build`:

```powershell
docker compose up -d
```

The legacy drift/training worker is intentionally excluded from the default
profile because its TensorFlow training image is several gigabytes:

```powershell
docker compose --profile training up -d traffic-drift-worker
```

Only rebuild images when dependency files or Dockerfiles change:

```powershell
docker compose build traffic-api
docker compose up -d traffic-api
```

Replace the development passwords in `.env` before sharing or deploying the
environment.

## Default local credentials

Defaults are intentionally limited to local development:

```text
MinIO:   minioadmin / minioadmin123
Airflow: admin / admin
```

## Verification

```powershell
docker compose ps
docker compose logs postgres minio-init mlflow airflow-init
```

Expected results:

- `postgres`, `minio`, `mlflow`, `airflow-webserver`, and
  `airflow-scheduler` are healthy.
- `minio-init` and `airflow-init` exit successfully.
- The existing API responds at `http://localhost:8000/health`.

## Operational notes

- PostgreSQL, MinIO, and Airflow logs use named Docker volumes.
- Removing containers does not remove named-volume data.
- `docker compose down -v` deletes local infrastructure data and must only be
  used when a full reset is intended.
- Phase 1 exposes service ports for local development. A deployed environment
  must keep PostgreSQL, MinIO API, MLflow, and Airflow behind private
  networking and authentication.
- The current legacy pipeline may still force MLflow to `file:./mlruns`.
  Connecting every training path to the shared MLflow server is handled in a
  later phase.

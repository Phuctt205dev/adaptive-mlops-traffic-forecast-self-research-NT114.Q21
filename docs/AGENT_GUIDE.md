# Agent Guide

This repository is a full traffic forecasting platform, not just a Docker wrapper. The goal of this guide is to let a human or AI agent understand the project quickly after cloning.

## What the project contains

- Backend API: `backend/app/` with FastAPI routers, services, DB session, auth, and health endpoints.
- Frontend: `frontend/` with the React/Vite dashboard.
- ML and training code: `src/` plus `retrain_job.py` and `scripts/training/`.
- Orchestration: `airflow/dags/` for training and retraining workflows.
- Database migrations: `alembic/`.
- Deployment and infra: `infra/`, `k8s/`, `docker-compose.yml`, `Dockerfile`, and frontend Docker files.
- Tests: `tests/`.
- Documentation: `docs/`.

## Main entrypoints

- Local API entrypoint: `app.py` re-exports `backend.app.main.app`.
- Docker API startup: `uvicorn app:app` from the backend container.
- Full stack local startup: `docker compose up --build`.
- Retraining job: `retrain_job.py`.
- Airflow DAGs: `airflow/dags/traffic_region_training.py` and related DAG files.

## Runtime boundaries

- Source code belongs in git.
- Generated outputs should usually stay out of git: `.venv/`, `frontend/node_modules/`, `mlruns/`, `results/`, `monitoring/`, and other rebuildable artifacts.
- Environment secrets should stay out of git. Use `.env.example` as the committed template.

## What someone should read first

1. `README.md`
2. `docs/AGENT_GUIDE.md`
3. `docker-compose.yml`
4. `backend/app/main.py`
5. `frontend/src/`
6. `src/`

## Practical structure

- API routes are under `backend/app/api/routes/`.
- Core platform services are under `backend/app/services/`.
- Data preprocessing and model code are in `src/`.
- Data snapshots and operational logs live in `data/`, `data_versions/`, and `monitoring/`, but only the files that are genuinely needed for reproducibility should be committed.

## Recommendation for git

Commit the source, config, docs, and example environment files. Avoid committing local runtime state, large generated artifacts, or machine-specific secrets. That keeps the repo understandable to both humans and agents after clone.
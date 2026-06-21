# Phase 1: Config

This phase separates local development settings from the future GitHub Pages +
AWS EKS deployment settings. It does not deploy anything yet.

## Target Deployment Shape

```text
GitHub Pages frontend
  -> HTTPS API endpoint
  -> AWS EKS traffic-api
  -> RDS PostgreSQL, S3, Airflow, MLflow
```

## Files Added Or Updated

- `.env.eks.example`: backend/EKS deployment variables and secret checklist.
- `frontend/.env.github-pages.example`: frontend GitHub Pages build variables.
- `frontend/.env.local.example`: local frontend build variables.
- `frontend/vite.config.js`: supports `VITE_BASE_PATH` for GitHub Pages.
- `backend/app/core/config.py`: local-only default CORS; production origins must
  come from `CORS_ORIGINS`.

## Required GitHub Pages Variables

For a repository site like:

```text
https://<github-user>.github.io/<repo-name>/
```

set:

```env
VITE_API_BASE_URL=https://api.example.com/api/v1
VITE_BASE_PATH=/<repo-name>/
```

For a custom domain served at `/`, set:

```env
VITE_BASE_PATH=/
```

## Required Backend CORS

The EKS backend must allow the GitHub Pages origin. Example:

```env
CORS_ORIGINS=["https://<github-user>.github.io","https://<github-user>.github.io/<repo-name>"]
```

Keep local origins only in local `.env`.

## Secrets That Must Not Be Committed

- `DATABASE_URL`
- `AUTH_SECRET_KEY`
- `INTERNAL_TRAINING_TOKEN`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `AIRFLOW_SECRET_KEY`
- `AIRFLOW_ADMIN_PASSWORD`
- `AIRFLOW_SQL_ALCHEMY_CONN`
- `MLFLOW_BACKEND_STORE_URI`
- AWS credentials, if not using IAM roles for service accounts

## Local Development

Local development can continue with:

```env
VITE_API_BASE_URL=http://localhost:8000/api/v1
VITE_BASE_PATH=/
```

and backend CORS:

```env
CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173","http://localhost:30073","http://127.0.0.1:30073"]
```

## Phase 1 Done Criteria

- Frontend build can receive API URL and base path from environment variables.
- Backend CORS does not rely on hardcoded public domains.
- EKS/GitHub Pages deployment variables are documented in example files.
- Real `.env.*` files are ignored by Git.

# Phase 3: Docker Image + ECR

This phase prepares production-friendly container images and an ECR push script.
It does not create AWS infrastructure yet.

## Images

| Image | Dockerfile | Purpose |
| --- | --- | --- |
| `traffic-api` | `infra/backend/Dockerfile` | FastAPI backend, training execution, inference API |
| `traffic-airflow` | `infra/airflow/Dockerfile` | Airflow image with DAGs baked in |
| `traffic-mlflow` | `infra/mlflow/Dockerfile` | MLflow tracking server |
| `traffic-web` | `frontend/Dockerfile.prod` | Optional static frontend image for Kubernetes |

The frontend is deployed to GitHub Pages in the current plan, so `traffic-web`
is optional for EKS. It is kept here in case the frontend later needs to run in
Kubernetes as well.

## ECR Repository Names

Recommended ECR repositories:

```text
traffic-mlops/traffic-api
traffic-mlops/traffic-airflow
traffic-mlops/traffic-mlflow
traffic-mlops/traffic-web
```

## Build Locally

From the repository root:

```powershell
docker build -t traffic-api:phase3 -f infra/backend/Dockerfile .
docker build -t traffic-airflow:phase3 -f infra/airflow/Dockerfile .
docker build -t traffic-mlflow:phase3 -f infra/mlflow/Dockerfile .
docker build `
  -t traffic-web:phase3 `
  -f frontend/Dockerfile.prod `
  --build-arg VITE_API_BASE_URL=https://api.example.com/api/v1 `
  --build-arg VITE_BASE_PATH=/ `
  frontend
```

## Push To ECR

Prerequisites:

- AWS CLI installed
- Docker running
- AWS credentials configured with ECR permissions

Create the repositories once:

```powershell
.\scripts\deploy\create_ecr_repositories.ps1 `
  -AwsRegion "ap-southeast-1" `
  -RepositoryPrefix "traffic-mlops"
```

Run:

```powershell
.\scripts\deploy\build_and_push_ecr.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -AwsRegion "ap-southeast-1" `
  -RepositoryPrefix "traffic-mlops" `
  -Tag "v1"
```

For frontend image builds:

```powershell
.\scripts\deploy\build_and_push_ecr.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -FrontendApiBaseUrl "https://api.example.com/api/v1" `
  -FrontendBasePath "/"
```

## Notes

- `.dockerignore` excludes local data, models, results, and Python caches from
  the image context.
- ECR repositories are created with scan-on-push enabled.
- Do not bake real secrets into images.
- AWS credentials should be supplied by ECR login locally, and later by IAM roles
  or Kubernetes secrets in EKS.

## Phase 3 Done Criteria

- Production Dockerfile exists for static frontend image.
- Airflow image can carry DAGs without local bind mounts.
- ECR repository creation script exists.
- ECR build/push script exists.
- Local image builds pass.

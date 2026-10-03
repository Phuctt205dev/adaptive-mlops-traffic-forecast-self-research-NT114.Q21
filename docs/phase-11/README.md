# Phase 11 - Kubernetes Deployment

## Implemented

Kubernetes manifests are stored in:

```text
k8s/base
```

Resources included:

- Namespace: `traffic-mlops`
- ConfigMap: app config and PostgreSQL init script.
- Secret example: local/demo credentials.
- PostgreSQL StatefulSet + PVC.
- MinIO StatefulSet + PVC + bucket init Job.
- MLflow Deployment + Service.
- Airflow init Job, webserver Deployment, scheduler Deployment, DAG ConfigMap.
- Traffic API Deployment + PVC + NodePort Service.
- React web Deployment + NodePort Service.
- Optional Kubernetes smoke test Job.

## Important Production Note

`k8s/base/secret.example.yaml` contains local demo values. Before production,
replace every value or create the secret manually:

```powershell
kubectl -n traffic-mlops create secret generic traffic-secrets `
  --from-literal=POSTGRES_PASSWORD="<strong-password>" `
  --from-literal=DATABASE_URL="postgresql+psycopg://traffic:<strong-password>@postgres:5432/traffic_management" `
  --from-literal=MLFLOW_BACKEND_STORE_URI="postgresql+psycopg2://traffic:<strong-password>@postgres:5432/mlflow" `
  --from-literal=AIRFLOW_SQL_ALCHEMY_CONN="postgresql+psycopg2://traffic:<strong-password>@postgres:5432/airflow" `
  --from-literal=MINIO_ROOT_USER="<minio-user>" `
  --from-literal=MINIO_ROOT_PASSWORD="<minio-password>" `
  --from-literal=AUTH_SECRET_KEY="<long-random-secret>" `
  --from-literal=BOOTSTRAP_ADMIN_PASSWORD="<admin-password>" `
  --from-literal=INTERNAL_TRAINING_TOKEN="<long-random-token>" `
  --from-literal=AIRFLOW_SECRET_KEY="<long-random-airflow-secret>" `
  --from-literal=AIRFLOW_ADMIN_PASSWORD="<airflow-password>"
```

If you create the secret manually, remove `secret.example.yaml` from
`k8s/base/kustomization.yaml` before applying.

## Build Local Images

For Docker Desktop Kubernetes:

```powershell
docker build -t traffic-api:local -f infra/backend/Dockerfile .
docker build -t traffic-mlflow:local -f infra/mlflow/Dockerfile .
docker build -t traffic-web:local ./frontend
```

For Minikube:

```powershell
minikube image build -t traffic-api:local -f infra/backend/Dockerfile .
minikube image build -t traffic-mlflow:local -f infra/mlflow/Dockerfile .
minikube image build -t traffic-web:local ./frontend
```

## Deploy

```powershell
kubectl apply -k k8s/base
```

Wait for core services:

```powershell
kubectl -n traffic-mlops get pods -w
```

If `kubectl apply` fails with:

```text
failed to download openapi: the server could not find the requested resource
```

the current Kubernetes context/API server is broken or points to the wrong
target. This is not a Kustomize render error. Verify with:

```powershell
kubectl kustomize k8s/base
kubectl config current-context
kubectl cluster-info
kubectl version
```

For Docker Desktop Kubernetes, fix the context:

```powershell
kubectl config get-contexts
kubectl config use-context docker-desktop
kubectl cluster-info
```

If Docker Desktop Kubernetes is disabled, open Docker Desktop and enable:

```text
Settings -> Kubernetes -> Enable Kubernetes
```

Then restart Docker Desktop and retry:

```powershell
kubectl apply -k k8s/base
```

`--validate=false` only skips schema validation. It will not fix a broken API
server/context.

Run Airflow init again if needed:

```powershell
kubectl -n traffic-mlops delete job airflow-init
kubectl apply -f k8s/base/airflow.yaml
```

## Access Locally

API NodePort:

```text
http://localhost:30080/api/v1/health/live
```

Web NodePort:

```text
http://localhost:30073
```

For MLflow, Airflow and MinIO, prefer port-forward in local clusters:

```powershell
kubectl -n traffic-mlops port-forward svc/mlflow 5000:5000
kubectl -n traffic-mlops port-forward svc/airflow-webserver 8080:8080
kubectl -n traffic-mlops port-forward svc/minio 9001:9001
```

Then open:

```text
MLflow:  http://localhost:5000
Airflow: http://localhost:8080
MinIO:   http://localhost:9001
```

## Run Smoke Test In Kubernetes

The smoke test Job is optional, so it is not included in the default
Kustomization resources.

Apply it after the API is ready:

```powershell
kubectl apply -f k8s/base/smoke-test-job.yaml
kubectl -n traffic-mlops wait --for=condition=complete job/traffic-smoke-test --timeout=180s
kubectl -n traffic-mlops logs job/traffic-smoke-test
```

Clean up the Job:

```powershell
kubectl -n traffic-mlops delete job traffic-smoke-test
```

## Useful Checks

```powershell
kubectl -n traffic-mlops get all
kubectl -n traffic-mlops get pvc
kubectl -n traffic-mlops logs deploy/traffic-api
kubectl -n traffic-mlops logs deploy/airflow-scheduler
kubectl -n traffic-mlops logs deploy/mlflow
```

Check API readiness:

```powershell
Invoke-RestMethod http://localhost:30080/api/v1/health/ready
```

## Scaling Notes

- `traffic-api` is set to `replicas: 1` because model artifacts are stored on a
  local PVC path and prediction loads active model files from disk.
- To scale API horizontally, move model artifact serving fully to object storage
  or use a ReadWriteMany volume.
- PostgreSQL, MinIO and Airflow in this phase are local/demo manifests, not
  production HA deployments.
- Rate limiting from Phase 10 is in-memory per pod. For multiple API replicas,
  replace it with Redis or ingress/API-gateway rate limiting.

## Delete Local Deployment

```powershell
kubectl delete -k k8s/base
```

PVCs may remain depending on cluster reclaim policy. Delete manually for a clean
local reset:

```powershell
kubectl -n traffic-mlops delete pvc --all
```

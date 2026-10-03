# Phase 8: Airflow + MLflow

This phase focuses on operating the MLOps services on EKS.

Airflow and MLflow are treated as internal tools. They are not exposed publicly
by default. Use `kubectl port-forward` when you need to inspect them.

## What This Phase Adds

- EKS Airflow patch sets new DAGs to active by default
- Deployment wait script for Airflow and MLflow
- Port-forward helper for local access
- Smoke test for:
  - MLflow health
  - Airflow health
  - `traffic_region_training` DAG visibility

## Files

```text
scripts/deploy/deploy_mlops_eks.ps1
scripts/deploy/port_forward_mlops.ps1
scripts/deploy/smoke_mlops_eks.ps1
k8s/overlays/eks/patches/airflow.eks.yaml
```

## Prerequisites

Complete these first:

- Phase 4: EKS infrastructure
- Phase 5: EKS add-ons
- Phase 6: EKS manifest
- Phase 7: API stack deployed

Airflow depends on Postgres. MLflow depends on Postgres and MinIO.

## Wait For MLOps Services

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/deploy_mlops_eks.ps1 `
  -AwsRegion "ap-southeast-1" `
  -ClusterName "traffic-mlops-dev"
```

This waits for:

- `deployment/mlflow`
- `job/airflow-init`
- `deployment/airflow-webserver`
- `deployment/airflow-scheduler`

## Open Airflow And MLflow Locally

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/port_forward_mlops.ps1
```

Then open:

```text
MLflow:  http://localhost:5001
Airflow: http://localhost:8081
```

The script prints process IDs. Stop port-forwarding later:

```powershell
Stop-Process -Id <mlflow-pid>,<airflow-pid>
```

## Smoke Test

Keep the port-forward processes running, then:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/smoke_mlops_eks.ps1 `
  -AirflowPassword "<airflow-admin-password>"
```

Expected checks:

- MLflow `/health` is reachable
- Airflow `/health` is reachable
- Airflow API can read DAG `traffic_region_training`

## Manual Checks

```powershell
kubectl -n traffic-mlops get pods
kubectl -n traffic-mlops logs deployment/mlflow --tail=100
kubectl -n traffic-mlops logs deployment/airflow-webserver --tail=100
kubectl -n traffic-mlops logs deployment/airflow-scheduler --tail=100
```

Check DAGs from inside the scheduler:

```powershell
kubectl -n traffic-mlops exec deployment/airflow-scheduler -- airflow dags list
```

Unpause manually if needed:

```powershell
kubectl -n traffic-mlops exec deployment/airflow-scheduler -- airflow dags unpause traffic_region_training
```

## Why Not Public Airflow/MLflow?

For this deployment plan, only the API should be public first. Airflow and
MLflow contain operational metadata, logs, model metadata and sometimes secrets
through environment/config views. Keeping them internal is simpler and safer.

Public HTTPS access can be added later behind authentication, but it is not
required for the website to work.

## Phase 8 Done Criteria

- MLflow deployment rolls out.
- Airflow init job completes.
- Airflow webserver rolls out.
- Airflow scheduler rolls out.
- MLflow health endpoint works through port-forward.
- Airflow health endpoint works through port-forward.
- `traffic_region_training` DAG is visible through Airflow API.

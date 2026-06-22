# Phase 6: Deploy Airflow Training Pipeline

This phase deploys Airflow on the existing k3s cluster.

It assumes Phase 5 is already running:

- PostgreSQL
- MinIO
- MLflow
- Traffic API

## What This Deploys

- Airflow database migration job
- Airflow webserver
- Airflow scheduler
- `traffic_region_training` DAG
- Airflow log PVC using k3s `local-path`

## Apply Airflow On Node 1

SSH to the k3s server node:

```powershell
ssh -i .\traffic-k3s-key.pem ubuntu@54.206.56.204
```

Pull the latest code:

```bash
cd ~/adaptive-mlops-traffic-forecast-self-research-NT114.Q21
git pull
```

Make sure the worker node has the compute label. Airflow webserver, scheduler and init job are pinned to this node.

```bash
kubectl label node traffic-k3s-worker traffic-role=compute --overwrite
```

Apply the Airflow overlay:

```bash
kubectl kustomize k8s/overlays/k3s-airflow --load-restrictor LoadRestrictionsNone | kubectl apply -f -
```

## Watch Airflow

```bash
kubectl get pods -n traffic-mlops -w
```

Expected Airflow pods/jobs:

- `airflow-init-*` should become `Completed`
- `airflow-webserver-*` should become `Running`
- `airflow-scheduler-*` should become `Running`

## Verify DAG

```bash
kubectl exec -n traffic-mlops deployment/airflow-scheduler -- airflow dags list | grep traffic_region_training
```

If the DAG is paused, unpause it:

```bash
kubectl exec -n traffic-mlops deployment/airflow-scheduler -- airflow dags unpause traffic_region_training
```

## Access Airflow UI

Temporarily open port `30081` in the k3s server EC2 security group for your IP `/32`.

Then open:

```text
http://54.206.56.204:30081
```

Default login from the current manifest:

```text
username: admin
password: admin
```

## Useful Debug Commands

```bash
kubectl logs -n traffic-mlops job/airflow-init
kubectl logs -n traffic-mlops deployment/airflow-webserver
kubectl logs -n traffic-mlops deployment/airflow-scheduler
kubectl describe pod -n traffic-mlops -l app=airflow-webserver
kubectl describe pod -n traffic-mlops -l app=airflow-scheduler
```

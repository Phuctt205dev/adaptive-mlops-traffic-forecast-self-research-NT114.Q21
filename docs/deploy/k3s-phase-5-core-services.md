# Phase 5: Deploy API + DB + MinIO + MLflow

This phase deploys the core backend stack to the EC2 k3s cluster.

Airflow is intentionally not deployed in this phase. It is handled in Phase 6.

## Services

- PostgreSQL
- MinIO
- MLflow
- Traffic API

## Apply Core Stack On Node 1

SSH to the k3s server node:

```powershell
ssh -i .\traffic-k3s-key.pem ubuntu@54.206.56.204
```

Label the worker node as the compute node. Traffic API is pinned there because the current training execution runs inside the API container through the internal training endpoint.

```bash
kubectl label node traffic-k3s-worker traffic-role=compute --overwrite
```

On the server, go to the repository directory and apply:

```bash
kubectl kustomize k8s/overlays/k3s-core --load-restrictor LoadRestrictionsNone | kubectl apply -f -
```

## Watch Pods

```bash
kubectl get pods -n traffic-mlops -w
```

Expected pods:

- `postgres-0`
- `minio-0`
- `minio-init-*`
- `mlflow-*`
- `traffic-api-*`

## Check Status

```bash
kubectl get all -n traffic-mlops
kubectl get pvc -n traffic-mlops
```

## Test API From Node 1

```bash
curl http://localhost:30080/api/v1/health/live
curl http://localhost:30080/api/v1/health/ready
```

Expected:

```json
{"status":"ok"}
```

and readiness:

```json
{"status":"ready", "...":"..."}
```

## Test API From Your Laptop

Before testing from your laptop, temporarily add this inbound rule to the k3s server EC2 security group:

- Type: Custom TCP
- Port: `30080`
- Source: your IP `/32`

This is only for direct NodePort testing. Later Phase 7 will expose the API through HTTPS on port `443`.

```powershell
Invoke-RestMethod "http://54.206.56.204:30080/api/v1/health/live"
Invoke-RestMethod "http://54.206.56.204:30080/api/v1/health/ready"
```

If this works, Phase 5 is complete.

## Useful Debug Commands

```bash
kubectl describe pod -n traffic-mlops <pod-name>
kubectl logs -n traffic-mlops deployment/traffic-api
kubectl logs -n traffic-mlops deployment/mlflow
kubectl logs -n traffic-mlops statefulset/postgres
kubectl logs -n traffic-mlops statefulset/minio
```

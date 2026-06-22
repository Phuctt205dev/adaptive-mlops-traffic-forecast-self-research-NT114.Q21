# Phase 4: k3s Kubernetes Manifests

This phase prepares Kubernetes manifests for the 2-node EC2 k3s cluster.

## What This Overlay Deploys

- PostgreSQL
- MinIO
- MLflow
- Airflow webserver and scheduler
- Traffic API

The React frontend is not deployed inside k3s because it is hosted by GitHub Pages.

## Docker Images

The overlay uses Docker Hub images:

- `docker.io/tannampham23/traffic-api:v1`
- `docker.io/tannampham23/traffic-mlflow:v1`
- `docker.io/tannampham23/traffic-airflow:v1`

## Important Files

- `k8s/overlays/k3s/kustomization.yaml`
- `k8s/overlays/k3s/secret.k3s.example.yaml`
- `k8s/overlays/k3s/patches/*.yaml`

## Render Manifests Locally

From the project root:

```powershell
kubectl kustomize k8s/overlays/k3s --load-restrictor LoadRestrictionsNone
```

## Apply On EC2 k3s Server

Copy or pull this repository on the k3s server node, then run from the project root:

```bash
kubectl kustomize k8s/overlays/k3s --load-restrictor LoadRestrictionsNone | kubectl apply -f -
```

## Verify

```bash
kubectl get ns
kubectl get all -n traffic-mlops
kubectl get pvc -n traffic-mlops
```

The first deploy can take several minutes because PostgreSQL, MinIO, MLflow, Airflow and API start in order.

## Public Ports For Initial Testing

- Traffic API: `http://54.206.56.204:30080/api/v1`
- Airflow: `http://54.206.56.204:30081`

HTTPS and domain routing are handled later in Phase 7.

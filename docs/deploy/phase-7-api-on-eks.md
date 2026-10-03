# Phase 7: API on EKS

This phase deploys the API stack to EKS and verifies that the public API is
reachable through AWS Load Balancer Controller.

## What This Phase Covers

- Render EKS manifest with real ECR registry and image tag
- Apply Kubernetes manifest to EKS
- Wait for API dependencies:
  - Postgres
  - MinIO
  - MLflow
  - Traffic API
- Wait for ALB hostname
- Smoke test:
  - `/api/v1/health/live`
  - `/api/v1/health/ready`
  - `/api/v1/auth/login`
  - `/api/v1/admin/system/summary`

## Prerequisites

Complete these first:

- Phase 3 images are pushed to ECR
- Phase 4 EKS infrastructure is applied
- Phase 5 EKS add-ons are installed
- Phase 6 EKS manifest overlay renders successfully

Tools:

```powershell
aws --version
kubectl version --client
helm version
```

## Deploy API Stack

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/deploy_api_eks.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -AwsRegion "ap-southeast-1" `
  -ClusterName "traffic-mlops-dev" `
  -RepositoryPrefix "traffic-mlops" `
  -ImageTag "v1" `
  -ApiBaseUrl "https://api.example.com/api/v1" `
  -FrontendOrigin "https://phuctt205dev.github.io"
```

The script prints the public API base URL after the ALB hostname is ready.

At this stage the URL is usually HTTP:

```text
http://<alb-hostname>/api/v1
```

HTTPS and custom domain are handled later in Phase 10.

## Smoke Test

Use the API URL printed by the deploy script:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/smoke_api_eks.ps1 `
  -ApiBaseUrl "http://<alb-hostname>/api/v1" `
  -AdminEmail "admin@example.com" `
  -AdminPassword "<bootstrap-admin-password>"
```

## Manual Checks

```powershell
kubectl -n traffic-mlops get pods
kubectl -n traffic-mlops get svc
kubectl -n traffic-mlops get ingress traffic-api
kubectl -n traffic-mlops logs deployment/traffic-api --tail=100
```

Check health directly:

```powershell
Invoke-RestMethod "http://<alb-hostname>/api/v1/health/live"
Invoke-RestMethod "http://<alb-hostname>/api/v1/health/ready"
```

## Common Issues

### ImagePullBackOff

Usually means the image tag does not exist in ECR, or the worker nodes cannot
pull from ECR.

Check:

```powershell
aws ecr describe-images `
  --repository-name traffic-mlops/traffic-api `
  --image-ids imageTag=v1 `
  --region ap-southeast-1
```

### API Not Ready

Readiness depends on Postgres, MinIO and MLflow. Check each:

```powershell
kubectl -n traffic-mlops get pods
kubectl -n traffic-mlops logs statefulset/postgres --tail=100
kubectl -n traffic-mlops logs statefulset/minio --tail=100
kubectl -n traffic-mlops logs deployment/mlflow --tail=100
```

### Ingress Has No Address

AWS Load Balancer Controller may not be installed or may not have IAM
permissions.

Check:

```powershell
kubectl -n kube-system get deployment aws-load-balancer-controller
kubectl -n kube-system logs deployment/aws-load-balancer-controller --tail=100
kubectl -n traffic-mlops describe ingress traffic-api
```

## Phase 7 Done Criteria

- API image pulls from ECR.
- `traffic-api` deployment rolls out successfully.
- ALB hostname is assigned to `traffic-api` ingress.
- `/api/v1/health/live` returns OK.
- `/api/v1/health/ready` returns ready.
- Admin login works through the public API URL.

# Phase 6: Kubernetes Manifest

This phase prepares an EKS-specific Kubernetes overlay.

The local Docker Desktop manifests remain in `k8s/base`. The EKS deployment
uses:

```text
k8s/overlays/eks
```

## What This Phase Adds

- EKS Kustomize overlay
- ECR image references for API, Airflow, MLflow and optional web image
- `gp3` StorageClass usage for PVCs
- ClusterIP services instead of local NodePort services
- AWS Load Balancer Controller Ingress for the API
- EKS resource requests and limits
- EKS secret template with placeholder values
- Render script for replacing AWS account, image tag and public URLs

## Important Files

```text
k8s/overlays/eks/kustomization.yaml
k8s/overlays/eks/ingress-api.yaml
k8s/overlays/eks/secret.eks.example.yaml
k8s/overlays/eks/patches/
scripts/deploy/render_eks_manifests.ps1
```

## Render Locally

From the repository root:

```powershell
kubectl kustomize k8s/overlays/eks
```

To render with your AWS account and image tag:

```powershell
.\scripts\deploy\render_eks_manifests.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -AwsRegion "ap-southeast-1" `
  -RepositoryPrefix "traffic-mlops" `
  -ImageTag "v1" `
  -ApiBaseUrl "https://api.example.com/api/v1" `
  -FrontendOrigin "https://phuctt205dev.github.io" `
  -OutputPath "k8s/rendered/eks.yaml"
```

If PowerShell blocks local scripts, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/render_eks_manifests.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -ImageTag "v1" `
  -OutputPath "k8s/rendered/eks.yaml"
```

`k8s/rendered/` is generated output and should not be committed.

## Before Applying

Do not apply the manifest with placeholder secrets.

Open:

```text
k8s/overlays/eks/secret.eks.example.yaml
```

Replace all values beginning with `replace-with-...`.

For a real production deployment, the better long-term option is AWS Secrets
Manager or External Secrets. For this student/demo deployment, Kubernetes Secret
is acceptable as long as real values are not committed.

## Apply Later

After Phase 4 and Phase 5 are applied to AWS:

```powershell
kubectl apply -f k8s/addons/storageclass-gp3.yaml
kubectl apply -f k8s/rendered/eks.yaml
```

Or directly:

```powershell
.\scripts\deploy\render_eks_manifests.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -ImageTag "v1" `
  -OutputPath "$env:TEMP\traffic-mlops-eks.yaml"

kubectl apply -f "$env:TEMP\traffic-mlops-eks.yaml"
```

## Verify

```powershell
kubectl -n traffic-mlops get pods
kubectl -n traffic-mlops get svc
kubectl -n traffic-mlops get ingress
kubectl -n traffic-mlops describe ingress traffic-api
```

The API public URL will come from the ALB address in:

```powershell
kubectl -n traffic-mlops get ingress traffic-api
```

## Phase 6 Done Criteria

- EKS overlay renders successfully.
- ECR image placeholders are present and replaceable.
- API is exposed through ALB Ingress.
- Stateful workloads use `gp3` PVCs.
- NodePort is removed from the EKS overlay.
- Secret values are templated and documented.

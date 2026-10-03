# Phase 5: EKS Add-ons

This phase prepares the operational add-ons required before deploying the
application workloads.

## What This Phase Adds

Managed by Terraform:

- EKS OIDC provider for IRSA
- `vpc-cni` EKS managed add-on
- `kube-proxy` EKS managed add-on
- `coredns` EKS managed add-on
- `aws-ebs-csi-driver` EKS managed add-on
- IAM role for EBS CSI
- IAM role for AWS Load Balancer Controller

Installed by script after the cluster exists:

- `gp3` default StorageClass
- Metrics Server
- AWS Load Balancer Controller

## Prerequisites

Phase 4 must be applied first:

```powershell
cd infra/aws/terraform
terraform apply
```

You also need:

- AWS CLI
- `kubectl`
- Helm

Check:

```powershell
aws sts get-caller-identity
kubectl version --client
helm version
```

## Apply Terraform Changes

From the Terraform folder:

```powershell
cd infra/aws/terraform
terraform init -upgrade
terraform plan
terraform apply
```

Get required outputs:

```powershell
terraform output vpc_id
terraform output load_balancer_controller_role_arn
```

## Install Cluster Add-ons

From the repository root:

```powershell
.\scripts\deploy\install_eks_addons.ps1 `
  -AwsRegion "ap-southeast-1" `
  -ClusterName "traffic-mlops-dev" `
  -VpcId "<terraform-vpc-id>" `
  -LoadBalancerControllerRoleArn "<terraform-load-balancer-controller-role-arn>"
```

Verify:

```powershell
kubectl get nodes
kubectl get storageclass
kubectl -n kube-system get pods
kubectl -n kube-system get deployment metrics-server
kubectl -n kube-system get deployment aws-load-balancer-controller
```

## Notes

- `gp3` is set as the default StorageClass for persistent workloads.
- AWS Load Balancer Controller is needed later for public HTTP/HTTPS ingress.
- Metrics Server is useful for `kubectl top` and future autoscaling.
- The Load Balancer Controller IAM policy is intentionally practical for this
  project. It can be tightened later in the security phase.

## Phase 5 Done Criteria

- Terraform validates with EKS add-on resources.
- OIDC provider and IRSA roles are defined.
- `gp3` StorageClass manifest exists.
- Add-on install script exists.
- After applying, `metrics-server` and `aws-load-balancer-controller` roll out
  successfully.

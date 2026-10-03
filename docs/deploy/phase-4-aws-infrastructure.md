# Phase 4: AWS Infrastructure

This phase prepares the AWS foundation for the EKS deployment.

It creates:

- VPC with two public subnets
- Internet Gateway and public route table
- ECR repositories for project images
- EKS cluster
- EKS managed node group
- IAM roles for EKS control plane and worker nodes

## Files

```text
infra/aws/terraform/
  versions.tf
  variables.tf
  main.tf
  outputs.tf
  terraform.tfvars.example
```

## Prerequisites

- AWS CLI installed
- Terraform installed
- AWS credentials configured
- An AWS account where you are allowed to create VPC, ECR, IAM and EKS resources

Check identity:

```powershell
aws sts get-caller-identity
```

## Configure

Copy the example variables:

```powershell
Copy-Item infra/aws/terraform/terraform.tfvars.example infra/aws/terraform/terraform.tfvars
```

Recommended before applying:

- Replace `admin_cidr_blocks = ["0.0.0.0/0"]` with your public IP CIDR, for example `["203.0.113.10/32"]`.
- Keep `node_desired_size = 2` for the full demo stack.
- Use `node_desired_size = 1` only when you are minimizing cost and are okay with weaker capacity.

## Create Infrastructure

```powershell
cd infra/aws/terraform
terraform init
terraform fmt
terraform validate
terraform plan
terraform apply
```

After apply, configure `kubectl`:

```powershell
aws eks update-kubeconfig --region ap-southeast-1 --name traffic-mlops-dev
kubectl get nodes
```

## ECR

Terraform creates the same ECR repositories as Phase 3:

```text
traffic-mlops/traffic-api
traffic-mlops/traffic-airflow
traffic-mlops/traffic-mlflow
traffic-mlops/traffic-web
```

Then push images:

```powershell
.\scripts\deploy\build_and_push_ecr.ps1 `
  -AwsAccountId "<aws-account-id>" `
  -AwsRegion "ap-southeast-1" `
  -RepositoryPrefix "traffic-mlops" `
  -Tag "v1"
```

Run that command from the repository root.

## Cost Note

EKS is not free. This phase creates billable resources:

- EKS control plane
- EC2 worker nodes
- EBS volumes used by nodes
- Data transfer and load balancers later

For a student/demo environment, destroy the stack when you are done:

```powershell
cd infra/aws/terraform
terraform destroy
```

## Phase 4 Done Criteria

- Terraform configuration exists for AWS foundation.
- ECR repositories are managed by Terraform.
- EKS cluster and node group are defined.
- `terraform validate` passes.
- User can run `terraform apply` and then `kubectl get nodes`.

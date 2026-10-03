output "aws_region" {
  description = "AWS region used by this stack."
  value       = var.aws_region
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint."
  value       = aws_eks_cluster.main.endpoint
}

output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public subnet IDs used by EKS."
  value       = values(aws_subnet.public)[*].id
}

output "load_balancer_controller_role_arn" {
  description = "IAM role ARN used by the AWS Load Balancer Controller service account."
  value       = aws_iam_role.load_balancer_controller.arn
}

output "ebs_csi_role_arn" {
  description = "IAM role ARN used by the EBS CSI controller service account."
  value       = aws_iam_role.ebs_csi.arn
}

output "ecr_repository_urls" {
  description = "ECR repository URLs keyed by repository name."
  value = {
    for name, repository in aws_ecr_repository.main : name => repository.repository_url
  }
}

output "update_kubeconfig_command" {
  description = "Command to configure kubectl for this EKS cluster."
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${aws_eks_cluster.main.name}"
}

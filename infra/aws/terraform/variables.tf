variable "project_name" {
  description = "Short project name used for AWS resource names."
  type        = string
  default     = "traffic-mlops"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for the infrastructure."
  type        = string
  default     = "ap-southeast-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the EKS VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones used by the VPC. Leave empty to use the first two available zones in the selected region."
  type        = list(string)
  default     = []
}

variable "cluster_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.30"
}

variable "node_instance_types" {
  description = "EC2 instance types for the default EKS managed node group."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_desired_size" {
  description = "Desired worker node count."
  type        = number
  default     = 2
}

variable "node_min_size" {
  description = "Minimum worker node count."
  type        = number
  default     = 1
}

variable "node_max_size" {
  description = "Maximum worker node count."
  type        = number
  default     = 3
}

variable "admin_cidr_blocks" {
  description = "CIDR blocks allowed to access the EKS public API endpoint. Use your public IP /32 for better security."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "ecr_repositories" {
  description = "ECR repositories used by this project."
  type        = list(string)
  default = [
    "traffic-mlops/traffic-api",
    "traffic-mlops/traffic-airflow",
    "traffic-mlops/traffic-mlflow",
    "traffic-mlops/traffic-web"
  ]
}

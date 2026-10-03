param(
    [string]$AwsRegion = "ap-southeast-1",

    [string]$ClusterName = "traffic-mlops-dev",

    [string]$Namespace = "traffic-mlops",

    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"

Write-Host "Updating kubeconfig for $ClusterName"
aws eks update-kubeconfig --region $AwsRegion --name $ClusterName

Write-Host "Waiting for MLflow"
kubectl -n $Namespace rollout status deployment/mlflow --timeout="$($TimeoutSeconds)s"

Write-Host "Waiting for Airflow init job"
kubectl -n $Namespace wait --for=condition=complete job/airflow-init --timeout="$($TimeoutSeconds)s"

Write-Host "Waiting for Airflow webserver"
kubectl -n $Namespace rollout status deployment/airflow-webserver --timeout="$($TimeoutSeconds)s"

Write-Host "Waiting for Airflow scheduler"
kubectl -n $Namespace rollout status deployment/airflow-scheduler --timeout="$($TimeoutSeconds)s"

Write-Host ""
Write-Host "MLOps services are ready."
Write-Host ""
Write-Host "Useful access commands:"
Write-Host "kubectl -n $Namespace port-forward svc/mlflow 5001:5000"
Write-Host "kubectl -n $Namespace port-forward svc/airflow-webserver 8081:8080"
Write-Host ""
Write-Host "Then open:"
Write-Host "MLflow:  http://localhost:5001"
Write-Host "Airflow: http://localhost:8081"

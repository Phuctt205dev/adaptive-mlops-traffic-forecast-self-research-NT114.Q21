param(
    [string]$Namespace = "traffic-mlops",

    [int]$MlflowLocalPort = 5001,

    [int]$AirflowLocalPort = 8081
)

$ErrorActionPreference = "Stop"

Write-Host "Starting MLflow port-forward on http://localhost:$MlflowLocalPort"
$mlflow = Start-Process `
    -FilePath "kubectl" `
    -ArgumentList @("-n", $Namespace, "port-forward", "svc/mlflow", "$($MlflowLocalPort):5000") `
    -PassThru `
    -WindowStyle Hidden

Write-Host "Starting Airflow port-forward on http://localhost:$AirflowLocalPort"
$airflow = Start-Process `
    -FilePath "kubectl" `
    -ArgumentList @("-n", $Namespace, "port-forward", "svc/airflow-webserver", "$($AirflowLocalPort):8080") `
    -PassThru `
    -WindowStyle Hidden

Write-Host ""
Write-Host "Port-forward processes started:"
Write-Host "MLflow PID:  $($mlflow.Id)"
Write-Host "Airflow PID: $($airflow.Id)"
Write-Host ""
Write-Host "Stop later with:"
Write-Host "Stop-Process -Id $($mlflow.Id),$($airflow.Id)"

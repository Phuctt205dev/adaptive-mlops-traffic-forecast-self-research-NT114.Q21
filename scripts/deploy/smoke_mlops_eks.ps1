param(
    [string]$MlflowUrl = "http://localhost:5001",

    [string]$AirflowUrl = "http://localhost:8081",

    [string]$AirflowUsername = "admin",

    [Parameter(Mandatory = $true)]
    [string]$AirflowPassword,

    [string]$DagId = "traffic_region_training",

    [int]$Retries = 30,

    [int]$DelaySeconds = 5
)

$ErrorActionPreference = "Stop"

function Wait-Check {
    param(
        [string]$Name,
        [scriptblock]$Check
    )

    for ($i = 1; $i -le $Retries; $i++) {
        try {
            $result = & $Check
            Write-Host "[smoke] $Name... ok"
            return $result
        } catch {
            if ($i -eq $Retries) {
                throw
            }
            Write-Host "[smoke] $Name... waiting ($i/$Retries)"
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

$authBytes = [Text.Encoding]::ASCII.GetBytes("$AirflowUsername`:$AirflowPassword")
$basicAuth = [Convert]::ToBase64String($authBytes)
$airflowHeaders = @{ Authorization = "Basic $basicAuth" }

Wait-Check "MLflow health" {
    Invoke-RestMethod -Uri "$($MlflowUrl.TrimEnd('/'))/health" -TimeoutSec 15
}

$airflowHealth = Wait-Check "Airflow health" {
    Invoke-RestMethod -Uri "$($AirflowUrl.TrimEnd('/'))/health" -TimeoutSec 15
}

$dag = Wait-Check "Airflow DAG $DagId" {
    Invoke-RestMethod `
        -Uri "$($AirflowUrl.TrimEnd('/'))/api/v1/dags/$DagId" `
        -Headers $airflowHeaders `
        -TimeoutSec 15
}

Write-Host ""
Write-Host "MLOps smoke test passed."
Write-Host "Airflow metadatabase: $($airflowHealth.metadatabase.status)"
Write-Host "Airflow scheduler: $($airflowHealth.scheduler.status)"
Write-Host "DAG id: $($dag.dag_id)"
Write-Host "DAG paused: $($dag.is_paused)"

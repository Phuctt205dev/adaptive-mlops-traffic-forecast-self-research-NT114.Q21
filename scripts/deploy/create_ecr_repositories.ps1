param(
    [string]$AwsRegion = "ap-southeast-1",

    [string]$RepositoryPrefix = "traffic-mlops"
)

$ErrorActionPreference = "Stop"

$repositories = @(
    "$RepositoryPrefix/traffic-api",
    "$RepositoryPrefix/traffic-airflow",
    "$RepositoryPrefix/traffic-mlflow",
    "$RepositoryPrefix/traffic-web"
)

foreach ($repository in $repositories) {
    $exists = aws ecr describe-repositories `
        --region $AwsRegion `
        --repository-names $repository `
        --query "repositories[0].repositoryName" `
        --output text 2>$null

    if ($LASTEXITCODE -eq 0 -and $exists -eq $repository) {
        Write-Host "ECR repository already exists: $repository"
        continue
    }

    Write-Host "Creating ECR repository: $repository"
    aws ecr create-repository `
        --region $AwsRegion `
        --repository-name $repository `
        --image-scanning-configuration scanOnPush=true `
        --encryption-configuration encryptionType=AES256 `
        --output table
}

Write-Host "Done."

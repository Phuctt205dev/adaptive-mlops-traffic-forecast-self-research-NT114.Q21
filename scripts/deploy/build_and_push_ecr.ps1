param(
    [Parameter(Mandatory = $true)]
    [string]$AwsAccountId,

    [string]$AwsRegion = "ap-southeast-1",

    [string]$RepositoryPrefix = "traffic-mlops",

    [string]$Tag = "v1",

    [string]$FrontendApiBaseUrl = "https://api.example.com/api/v1",

    [string]$FrontendBasePath = "/"
)

$ErrorActionPreference = "Stop"

$registry = "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com"
$images = @(
    @{
        Name = "traffic-api"
        Repository = "$RepositoryPrefix/traffic-api"
        Dockerfile = "infra/backend/Dockerfile"
        Context = "."
        BuildArgs = @()
    },
    @{
        Name = "traffic-airflow"
        Repository = "$RepositoryPrefix/traffic-airflow"
        Dockerfile = "infra/airflow/Dockerfile"
        Context = "."
        BuildArgs = @()
    },
    @{
        Name = "traffic-mlflow"
        Repository = "$RepositoryPrefix/traffic-mlflow"
        Dockerfile = "infra/mlflow/Dockerfile"
        Context = "."
        BuildArgs = @()
    },
    @{
        Name = "traffic-web"
        Repository = "$RepositoryPrefix/traffic-web"
        Dockerfile = "frontend/Dockerfile.prod"
        Context = "frontend"
        BuildArgs = @(
            "--build-arg", "VITE_API_BASE_URL=$FrontendApiBaseUrl",
            "--build-arg", "VITE_BASE_PATH=$FrontendBasePath"
        )
    }
)

Write-Host "Logging in to ECR registry $registry"
aws ecr get-login-password --region $AwsRegion |
    docker login --username AWS --password-stdin $registry

foreach ($image in $images) {
    $remoteImage = "$registry/$($image.Repository):$Tag"
    $latestImage = "$registry/$($image.Repository):latest"
    $buildArgs = @(
        "build",
        "-f", $image.Dockerfile,
        "-t", $remoteImage,
        "-t", $latestImage
    ) + $image.BuildArgs + @($image.Context)

    Write-Host "Building $($image.Name) -> $remoteImage"
    docker @buildArgs

    Write-Host "Pushing $remoteImage"
    docker push $remoteImage
    docker push $latestImage
}

Write-Host "Done. Image tag: $Tag"

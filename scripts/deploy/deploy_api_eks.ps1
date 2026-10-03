param(
    [Parameter(Mandatory = $true)]
    [string]$AwsAccountId,

    [string]$AwsRegion = "ap-southeast-1",

    [string]$ClusterName = "traffic-mlops-dev",

    [string]$RepositoryPrefix = "traffic-mlops",

    [string]$ImageTag = "v1",

    [string]$ApiBaseUrl = "https://api.example.com/api/v1",

    [string]$FrontendOrigin = "https://phuctt205dev.github.io",

    [string]$ManifestPath = ""
)

$ErrorActionPreference = "Stop"

$RepositoryRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$RenderScript = Join-Path $PSScriptRoot "render_eks_manifests.ps1"
$StorageClassManifest = Join-Path $RepositoryRoot "k8s\addons\storageclass-gp3.yaml"

if (-not $ManifestPath) {
    $ManifestPath = Join-Path $env:TEMP "traffic-mlops-eks-$ImageTag.yaml"
}

Write-Host "Updating kubeconfig for $ClusterName"
aws eks update-kubeconfig --region $AwsRegion --name $ClusterName

Write-Host "Rendering EKS manifest"
powershell -NoProfile -ExecutionPolicy Bypass -File $RenderScript `
    -AwsAccountId $AwsAccountId `
    -AwsRegion $AwsRegion `
    -RepositoryPrefix $RepositoryPrefix `
    -ImageTag $ImageTag `
    -ApiBaseUrl $ApiBaseUrl `
    -FrontendOrigin $FrontendOrigin `
    -OutputPath $ManifestPath

Write-Host "Applying storage class"
kubectl apply -f $StorageClassManifest

Write-Host "Applying API stack manifest"
kubectl apply -f $ManifestPath

Write-Host "Waiting for API dependencies"
kubectl -n traffic-mlops rollout status statefulset/postgres --timeout=300s
kubectl -n traffic-mlops rollout status statefulset/minio --timeout=300s
kubectl -n traffic-mlops rollout status deployment/mlflow --timeout=300s
kubectl -n traffic-mlops rollout status deployment/traffic-api --timeout=600s

Write-Host "Waiting for API ingress address"
$ingressHost = ""
for ($i = 1; $i -le 60; $i++) {
    $ingressHost = kubectl -n traffic-mlops get ingress traffic-api -o jsonpath="{.status.loadBalancer.ingress[0].hostname}" 2>$null
    if ($ingressHost) {
        break
    }
    Start-Sleep -Seconds 10
}

if (-not $ingressHost) {
    throw "Timed out waiting for ALB hostname on ingress traffic-api."
}

$publicApiBaseUrl = "http://$ingressHost/api/v1"

Write-Host ""
Write-Host "API ingress hostname: $ingressHost"
Write-Host "Public API base URL: $publicApiBaseUrl"
Write-Host ""
Write-Host "Run smoke test:"
Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/smoke_api_eks.ps1 -ApiBaseUrl `"$publicApiBaseUrl`" -AdminPassword `"<bootstrap-admin-password>`""

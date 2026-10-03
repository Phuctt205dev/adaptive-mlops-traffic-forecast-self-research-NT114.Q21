param(
    [Parameter(Mandatory = $true)]
    [string]$AwsAccountId,

    [string]$AwsRegion = "ap-southeast-1",

    [string]$RepositoryPrefix = "traffic-mlops",

    [string]$ImageTag = "v1",

    [string]$ApiBaseUrl = "https://api.example.com/api/v1",

    [string]$FrontendOrigin = "https://phuctt205dev.github.io",

    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$registry = "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com"
$manifest = kubectl kustomize k8s/overlays/eks

$manifest = $manifest.Replace("000000000000.dkr.ecr.ap-southeast-1.amazonaws.com/traffic-mlops", "$registry/$RepositoryPrefix")
$manifest = $manifest.Replace(":v1", ":$ImageTag")
$manifest = $manifest.Replace("https://api.example.com/api/v1", $ApiBaseUrl)
$manifest = $manifest.Replace("https://phuctt205dev.github.io", $FrontendOrigin)

if ($OutputPath) {
    $directory = Split-Path -Parent $OutputPath
    if ($directory) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    Set-Content -Path $OutputPath -Value $manifest -Encoding utf8
    Write-Host "Rendered EKS manifest: $OutputPath"
} else {
    $manifest
}

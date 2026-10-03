param(
    [string]$AwsRegion = "ap-southeast-1"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "Required command 'aws' was not found."
}

Write-Host "[eks] Clusters in ${AwsRegion}:"
aws eks list-clusters --region $AwsRegion --query "clusters[]" --output table
if ($LASTEXITCODE -ne 0) {
    throw "Could not list EKS clusters in region '$AwsRegion'."
}

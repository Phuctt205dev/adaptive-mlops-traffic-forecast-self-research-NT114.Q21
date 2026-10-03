param(
    [Parameter(Mandatory = $true)]
    [string]$ApiDomainName,

    [string]$AwsRegion = "ap-southeast-1",
    [string]$OutputPath = "k8s/rendered/acm-certificate-validation.json",
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found."
    }
}

Assert-Command aws

$tokenSource = ($ApiDomainName -replace "[^A-Za-z0-9]", "")
if ($tokenSource.Length -gt 32) {
    $tokenSource = $tokenSource.Substring(0, 32)
}
if (-not $tokenSource) {
    $tokenSource = "trafficmlops"
}

Write-Host "[acm] Requesting certificate for $ApiDomainName in $AwsRegion..."
$certificateArn = aws acm request-certificate `
    --domain-name $ApiDomainName `
    --validation-method DNS `
    --idempotency-token $tokenSource `
    --region $AwsRegion `
    --query CertificateArn `
    --output text

if (-not $certificateArn -or $certificateArn -eq "None") {
    throw "Could not request ACM certificate."
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$certificate = $null
$record = $null

while ((Get-Date) -lt $deadline) {
    $certificateJson = aws acm describe-certificate `
        --certificate-arn $certificateArn `
        --region $AwsRegion `
        --output json
    $certificate = $certificateJson | ConvertFrom-Json
    $record = $certificate.Certificate.DomainValidationOptions[0].ResourceRecord
    if ($record) {
        break
    }
    Start-Sleep -Seconds 5
}

if (-not $record) {
    throw "ACM did not return a DNS validation record within $TimeoutSeconds seconds."
}

$result = [ordered]@{
    api_domain_name = $ApiDomainName
    certificate_arn = $certificateArn
    validation_record = [ordered]@{
        type = $record.Type
        name = $record.Name
        value = $record.Value
    }
}

$directory = Split-Path -Parent $OutputPath
if ($directory) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
$result | ConvertTo-Json -Depth 6 | Set-Content -Path $OutputPath -Encoding utf8

Write-Host ""
Write-Host "[acm] Certificate ARN:"
Write-Host $certificateArn
Write-Host ""
Write-Host "[dynu] Add this DNS validation CNAME in Dynu:"
Write-Host "Type : $($record.Type)"
Write-Host "Name : $($record.Name)"
Write-Host "Value: $($record.Value)"
Write-Host ""
Write-Host "[acm] Saved validation details to $OutputPath"
Write-Host "[acm] After adding the CNAME, wait until ACM status becomes ISSUED."

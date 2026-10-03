param(
    [Parameter(Mandatory = $true)]
    [string]$CertificateArn,

    [string]$AwsRegion = "ap-southeast-1"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "Required command 'aws' was not found."
}

$certificate = aws acm describe-certificate `
    --certificate-arn $CertificateArn `
    --region $AwsRegion `
    --output json | ConvertFrom-Json

$status = $certificate.Certificate.Status
Write-Host "[acm] Status: $status"

if ($certificate.Certificate.DomainValidationOptions[0].ResourceRecord) {
    $record = $certificate.Certificate.DomainValidationOptions[0].ResourceRecord
    Write-Host "[acm] Validation CNAME:"
    Write-Host "Type : $($record.Type)"
    Write-Host "Name : $($record.Name)"
    Write-Host "Value: $($record.Value)"
}

if ($status -ne "ISSUED") {
    exit 1
}

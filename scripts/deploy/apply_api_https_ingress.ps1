param(
    [Parameter(Mandatory = $true)]
    [string]$ApiDomainName,

    [Parameter(Mandatory = $true)]
    [string]$CertificateArn,

    [string]$AwsRegion = "ap-southeast-1",
    [string]$ClusterName = "traffic-mlops-dev",
    [string]$Namespace = "traffic-mlops",
    [string]$FrontendOrigin = "https://phuctt205dev.github.io"
)

$ErrorActionPreference = "Stop"

function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found."
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

Assert-Command aws
Assert-Command kubectl

$apiBaseUrl = "https://$ApiDomainName/api/v1"
$listenPorts = '[{"HTTP":80},{"HTTPS":443}]'

Write-Host "[eks] Checking cluster $ClusterName in $AwsRegion..."
aws eks describe-cluster --region $AwsRegion --name $ClusterName --query "cluster.name" --output text | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[eks] Existing clusters in ${AwsRegion}:"
    aws eks list-clusters --region $AwsRegion --query "clusters[]" --output table
    throw "EKS cluster '$ClusterName' was not found in region '$AwsRegion'. Pass the real cluster name with -ClusterName, or complete Phase 4 first."
}

Write-Host "[eks] Updating kubeconfig for $ClusterName..."
Invoke-Checked aws eks update-kubeconfig --region $AwsRegion --name $ClusterName | Out-Null

Write-Host "[ingress] Checking traffic-api ingress..."
kubectl -n $Namespace get ingress traffic-api | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[k8s] Ingresses in namespace ${Namespace}:"
    kubectl -n $Namespace get ingress
    throw "Ingress 'traffic-api' was not found in namespace '$Namespace'. Deploy Phase 7 API stack first."
}

Write-Host "[ingress] Applying HTTPS annotations..."
Invoke-Checked kubectl -n $Namespace annotate ingress traffic-api `
    "alb.ingress.kubernetes.io/listen-ports=$listenPorts" `
    "alb.ingress.kubernetes.io/certificate-arn=$CertificateArn" `
    "alb.ingress.kubernetes.io/ssl-redirect=443" `
    "alb.ingress.kubernetes.io/ssl-policy=ELBSecurityPolicy-TLS13-1-2-2021-06" `
    --overwrite | Out-Null

$ingressPatch = @{
    spec = @{
        rules = @(
            @{
                host = $ApiDomainName
                http = @{
                    paths = @(
                        @{
                            path = "/api/v1"
                            pathType = "Prefix"
                            backend = @{
                                service = @{
                                    name = "traffic-api"
                                    port = @{
                                        number = 8000
                                    }
                                }
                            }
                        }
                    )
                }
            }
        )
    }
} | ConvertTo-Json -Depth 12 -Compress

$ingressPatchPath = Join-Path $env:TEMP "traffic-api-ingress-https-patch.json"
Set-Content -Path $ingressPatchPath -Value $ingressPatch -Encoding utf8
Invoke-Checked kubectl -n $Namespace patch ingress traffic-api --type=merge --patch-file $ingressPatchPath | Out-Null

Write-Host "[config] Updating API base URL and CORS origin..."
$corsJson = @($FrontendOrigin) | ConvertTo-Json -Compress
$configPatch = @{
    data = @{
        VITE_API_BASE_URL = $apiBaseUrl
        CORS_ORIGINS = $corsJson
    }
} | ConvertTo-Json -Depth 6 -Compress

$configPatchPath = Join-Path $env:TEMP "traffic-config-api-https-patch.json"
Set-Content -Path $configPatchPath -Value $configPatch -Encoding utf8
Invoke-Checked kubectl -n $Namespace patch configmap traffic-config --type=merge --patch-file $configPatchPath | Out-Null
Invoke-Checked kubectl -n $Namespace rollout restart deployment/traffic-api | Out-Null
Invoke-Checked kubectl -n $Namespace rollout status deployment/traffic-api --timeout=600s

Write-Host "[ingress] Waiting for ALB hostname..."
$albHostname = ""
for ($i = 0; $i -lt 60; $i++) {
    $albHostname = kubectl -n $Namespace get ingress traffic-api -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read ingress 'traffic-api' in namespace '$Namespace'."
    }
    if ($albHostname) {
        break
    }
    Start-Sleep -Seconds 10
}

if (-not $albHostname) {
    throw "Ingress did not return an ALB hostname."
}

Write-Host ""
Write-Host "[dynu] Point your API domain to this ALB hostname:"
Write-Host "Host : $ApiDomainName"
Write-Host "Type : CNAME"
Write-Host "Value: $albHostname"
Write-Host ""
Write-Host "[api] HTTPS API base URL:"
Write-Host $apiBaseUrl
Write-Host ""
Write-Host "[next] After DNS propagates, run:"
Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/smoke_api_eks.ps1 -ApiBaseUrl `"$apiBaseUrl`" -AdminPassword `"<bootstrap-admin-password>`""

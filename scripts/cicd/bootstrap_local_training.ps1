[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Apply')][string]$Action = 'Plan',
    [Parameter(Mandatory = $true)][string]$Kubeconfig,
    [Parameter(Mandatory = $true)][string]$Context,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F-]{36}$')][string]$NamespaceUid,
    [ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ReviewedSha256
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$manifest = Join-Path $root 'deployment\rke2\local-training-rbac.yaml'
$digest = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
if ($Action -eq 'Apply' -and (!$ReviewedSha256 -or $ReviewedSha256.ToLowerInvariant() -ne $digest)) {
    throw 'Review the fixed bootstrap manifest and supply its exact SHA256 from Plan before Apply.'
}
$configPath = (Resolve-Path -LiteralPath $Kubeconfig).Path
$kubectl = (Get-Command kubectl -CommandType Application -ErrorAction SilentlyContinue).Source
if (!$kubectl) {
    $kubectl = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe'
    if (!(Test-Path -LiteralPath $kubectl -PathType Leaf)) { throw 'kubectl is unavailable.' }
}
$connection = @('--kubeconfig', $configPath, '--context', $Context, '--request-timeout=20s', '-n', 'traffic-mlops')
$observed = & $kubectl @connection get namespace traffic-mlops -o 'jsonpath={.metadata.uid}'
if ($LASTEXITCODE -ne 0 -or (($observed -join '').Trim() -ne $NamespaceUid)) { throw 'Namespace identity check failed.' }
Write-Output ('Bootstrap manifest SHA256: ' + $digest)
# The cluster owner uses their own admin context. CI delivery/runtime tokens
# are never upgraded or copied into a workload by this helper.
& $kubectl @connection apply --dry-run=server -f $manifest -o name
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap server dry-run failed. Nothing was applied.' }
if ($Action -eq 'Apply') {
    if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -ne $digest) {
        throw 'The bootstrap manifest changed after review.'
    }
    & $kubectl @connection apply -f $manifest -o name
    if ($LASTEXITCODE -ne 0) { throw 'Bootstrap did not complete. Inspect the named resources before retrying.' }
    Write-Output 'Bootstrap applied. Local remains disabled until a reviewed application release enables it.'
} else {
    Write-Output 'Plan only. No permissions were changed; no Job or training was created.'
}

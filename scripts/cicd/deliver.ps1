[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArtifactDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedCommit,
    [ValidateSet('Plan', 'Apply', 'Verify', 'Rollback')][string]$Action = 'Apply',
    [ValidateSet('release', 'migrate-role-runtime-v1')][string]$Operation = 'release'
)
$ErrorActionPreference = 'Stop'
if (-not $env:RKE2_CONTEXT -or -not $env:RKE2_NAMESPACE_UID -or -not $env:RKE2_KUBECONFIG) {
    throw 'Set RKE2_CONTEXT, RKE2_NAMESPACE_UID and the dedicated RKE2_KUBECONFIG path.'
}
$python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$kubectl = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe'
if (-not (Test-Path -LiteralPath $kubectl -PathType Leaf)) {
    $kubectl = (Get-Command kubectl -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
}
$artifactRoot = (Resolve-Path -LiteralPath $ArtifactDirectory).Path
$delivery = Get-Content -LiteralPath (Join-Path $artifactRoot 'delivery.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($delivery.commit -ne $ExpectedCommit -or $delivery.image -notmatch '^ghcr\.io/[a-z0-9./_-]+@sha256:[0-9a-f]{64}$') {
    throw 'The image/commit receipt is invalid.'
}
# delivery.json is a CI receipt outside the image context. Verify the remaining
# package in a separate directory so no checked artifact is modified in place.
$checkedRoot = Join-Path $env:RUNNER_TEMP ('traffic-release-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $checkedRoot | Out-Null
Get-ChildItem -LiteralPath $artifactRoot -Force | Where-Object Name -ne 'delivery.json' | Copy-Item -Destination $checkedRoot -Recurse
& $python -B scripts/cicd/release.py Verify --directory $checkedRoot --commit $ExpectedCommit
if ($LASTEXITCODE -ne 0) { throw 'Release file verification failed.' }
$manifest = Get-Content -LiteralPath (Join-Path $checkedRoot 'release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.release_id -ne $delivery.release_id) { throw 'Image receipt and release identity differ.' }
$receiptDirectory = Join-Path (Get-Location).Path 'results\cicd'
New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
$journal = Join-Path $receiptDirectory ('delivery-' + $ExpectedCommit + '-' + $env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT + '.json')
if ($manifest.schema -ne 'traffic-release/v2' -or $delivery.schema -ne 'traffic-delivery/v2') {
    throw 'The unified engine requires a checked role release. Recover a legacy transaction with its original controller/artifact.'
}
$deliveryScript = 'scripts/cicd/roles_deploy.py'
$deliveryArgs = @('--directory', $checkedRoot, '--binding', (Join-Path $artifactRoot 'delivery.json'),
    '--commit', $ExpectedCommit, '--journal', $journal, '--kubectl', $kubectl,
    '--context', $env:RKE2_CONTEXT, '--namespace-uid', $env:RKE2_NAMESPACE_UID, '--kubeconfig', $env:RKE2_KUBECONFIG)
if ($Operation -eq 'migrate-role-runtime-v1') {
    if ($Action -eq 'Apply' -and $env:DELIVERY_MODE -ne 'manual') { throw 'Runtime migration requires the Manual gate.' }
    $deliveryArgs += @('--migration', 'role-runtime-v1')
}
& $python -B $deliveryScript $Action @deliveryArgs
if ($LASTEXITCODE -ne 0) { throw 'Delivery operation failed. Inspect its receipt before starting another release.' }
if ($Action -eq 'Apply') {
    & $python -B $deliveryScript Verify @deliveryArgs
    if ($LASTEXITCODE -ne 0) { throw 'Post-delivery verification failed.' }
}

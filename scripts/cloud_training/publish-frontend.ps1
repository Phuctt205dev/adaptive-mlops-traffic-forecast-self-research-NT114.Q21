[CmdletBinding()]
param([switch]$Publish, [string]$Kubectl = '')
$ErrorActionPreference = 'Stop'
$cloudRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not $Kubectl) {
    $cloudCommand = Get-Command kubectl -ErrorAction SilentlyContinue
    if ($cloudCommand) { $Kubectl = $cloudCommand.Source }
    else { $Kubectl = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe' }
}
$cloudParameters = @('--review', (Join-Path $cloudRoot 'results\cloud-training\review-final'))
if ($Publish) {
    $cloudParameters += @('--publish', '--kubectl', $Kubectl, '--snapshot', (Join-Path $cloudRoot 'results\cloud-training\snapshot-20261028-v2.json'))
}
& (Join-Path $cloudRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'publish_frontend.py') @cloudParameters
if ($LASTEXITCODE -ne 0) { throw 'Frontend publish check failed. Stop here and inspect the sanitized summary; do not repeat a partially completed commit/push.' }

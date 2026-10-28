# Default action checks only. The operator chooses each mutating action explicitly.
[CmdletBinding()]
param(
    [ValidateSet('Preflight', 'Credentials', 'Stage', 'Enable', 'Verify', 'InspectRun', 'Rollback')]
    [string]$Action = 'Preflight',
    [ValidateSet('', 'enabled', 'disabled')][string]$Expect = '',
    [string]$RunId = '',
    [ValidateSet('', 'completed', 'failed')][string]$RunState = '',
    [string]$Kubectl = '',
    [string]$ReviewDirectory = '',
    [string]$Snapshot = ''
)
$ErrorActionPreference = 'Stop'
$cloudRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$cloudPython = Join-Path $cloudRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $cloudPython)) { throw 'Project .venv is missing.' }
if (-not $Kubectl) {
    $cloudCommand = Get-Command kubectl -ErrorAction SilentlyContinue
    if ($cloudCommand) { $Kubectl = $cloudCommand.Source }
    else { $Kubectl = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe' }
}
if (-not (Test-Path -LiteralPath $Kubectl)) { throw 'kubectl was not found. Supply -Kubectl with its path.' }
if (-not $ReviewDirectory) { $ReviewDirectory = Join-Path $cloudRoot 'results\cloud-training\review-final' }
if (-not $Snapshot) { $Snapshot = Join-Path $cloudRoot 'results\cloud-training\snapshot-20261028-v2.json' }
$cloudParameters = @('--action', $Action, '--kubectl', $Kubectl, '--review', $ReviewDirectory, '--snapshot', $Snapshot)
if ($Expect) { $cloudParameters += @('--expect', $Expect) }
if ($RunId) { $cloudParameters += @('--run-id', $RunId) }
if ($RunState) { $cloudParameters += @('--run-state', $RunState) }
& $cloudPython (Join-Path $PSScriptRoot 'manage_rollout.py') @cloudParameters
if ($LASTEXITCODE -ne 0) { throw "$Action failed. Stop here; share only the sanitized summary and keep the rollout journal." }

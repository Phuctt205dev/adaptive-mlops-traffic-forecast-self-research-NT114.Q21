[CmdletBinding()]
param(
    [ValidateSet('Check', 'Apply', 'Verify', 'Rollback')][string]$Action = 'Check',
    [string]$Kubectl = ''
)
$ErrorActionPreference = 'Stop'
$cloudRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not $Kubectl) {
    $cloudCommand = Get-Command kubectl -ErrorAction SilentlyContinue
    if ($cloudCommand) { $Kubectl = $cloudCommand.Source }
    else { $Kubectl = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe' }
}
& (Join-Path $cloudRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'result_hotfix.py') --action $Action --kubectl $Kubectl
if ($LASTEXITCODE -ne 0) { throw "$Action failed. Stop here and keep result-fix-journal.json; share only the sanitized summary." }

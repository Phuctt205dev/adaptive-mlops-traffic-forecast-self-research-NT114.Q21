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
& (Join-Path $cloudRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'network_hotfix.py') --action $Action --kubectl $Kubectl
if ($LASTEXITCODE -ne 0) { throw "$Action failed. Stop here; keep the hotfix journal and share the sanitized summary." }

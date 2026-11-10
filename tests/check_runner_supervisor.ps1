# Offline behavior checks. No registry, runner or production mutations.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\cicd\runner.ps1')
$script:listeners = @()
$script:workers = @()
$script:starts = 0
function Get-RunnerProcesses([string]$Name) {
    if ($Name -eq 'Runner.Listener') { return $script:listeners }
    return $script:workers
}
function Start-ManagedRunner { $script:starts += 1 }

$script:listeners = @('existing listener')
if ((Invoke-RunnerTick) -ne 'listener_present' -or $script:starts -ne 0) { throw 'An existing listener must be preserved.' }
$script:listeners = @(); $script:workers = @('in-flight worker')
if ((Invoke-RunnerTick) -ne 'worker_without_listener' -or $script:starts -ne 0) { throw 'An in-flight worker must block a replacement listener.' }
$script:workers = @()
if ((Invoke-RunnerTick) -ne 'listener_start_requested' -or $script:starts -ne 1) { throw 'An absent runner must be started.' }
$command = Get-RunnerCommand
if ($command.Length -gt 260 -or $command -notlike '*-WindowStyle Hidden*' -or $command -notlike '*-Action Run') { throw 'Invalid per-user startup command.' }
Write-Output 'Runner supervisor offline checks passed: existing listener, surviving worker, restart and startup command.'

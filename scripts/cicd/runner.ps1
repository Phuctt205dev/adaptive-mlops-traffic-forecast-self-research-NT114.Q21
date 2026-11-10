# Per-user runner supervision. No registration, credentials or cluster changes.
[CmdletBinding()]
param([ValidateSet('Check', 'Install', 'Run', 'Uninstall')][string]$Action = 'Check')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$rootConfig = Join-Path $PSScriptRoot 'supervisor-root.json'
if (Test-Path -LiteralPath $rootConfig) {
    $projectRoot = (Get-Content -LiteralPath $rootConfig -Raw | ConvertFrom-Json).project_root
}
$runnerDirectory = Join-Path $projectRoot 'results\cicd\actions-runner'
$stateDirectory = Join-Path $projectRoot 'results\cicd\runner-supervisor'
$installedScript = Join-Path $stateDirectory 'runner.ps1'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$entryName = 'TrafficMLOpsCDRunner'
$stopFile = Join-Path $stateDirectory 'stop'

function Get-RunnerCommand([string]$ScriptPath = $installedScript) {
    # The Windows Run key limits its command to 260 characters.
    $command = 'powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $ScriptPath + '" -Action Run'
    if ($command.Length -gt 260) { throw 'Runner startup path exceeds the Windows Run-key limit.' }
    return $command
}

function Get-RunnerStartup {
    if (-not (Test-Path -LiteralPath $runKey)) { return $null }
    return (Get-Item -LiteralPath $runKey).GetValue($entryName, $null)
}

function Get-RunnerProcesses([string]$Name) {
    return @(Get-Process -Name $Name -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and (Split-Path (Split-Path $_.Path -Parent) -Parent) -eq $runnerDirectory
    })
}

function Test-RunnerRegistration {
    $path = Join-Path $runnerDirectory '.runner'
    if (-not (Test-Path -LiteralPath $path)) { throw 'Register the existing deployment runner first.' }
    $configuration = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ($configuration.gitHubUrl.TrimEnd('/') -ne 'https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21') {
        throw 'Runner repository differs; startup was not changed.'
    }
    return $configuration.agentId
}

function Start-ManagedRunner {
    # Existing setup preserves Windows CA roots and process-scoped Git longpaths.
    & (Join-Path $projectRoot 'scripts\cicd\setup.ps1') -Action StartRunner | Out-Null
}

function Invoke-RunnerTick {
    if (@(Get-RunnerProcesses 'Runner.Listener').Count -gt 0) { return 'listener_present' }
    # A surviving worker may still own an in-flight release. Never interrupt it.
    if (@(Get-RunnerProcesses 'Runner.Worker').Count -gt 0) { return 'worker_without_listener' }
    Start-ManagedRunner
    return 'listener_start_requested'
}

function Write-RunnerStatus([string]$State) {
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $temporary = Join-Path $stateDirectory ('status-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $value = @{ schema = 'traffic-runner-supervisor/v1'; observed_at = [DateTime]::UtcNow.ToString('o');
                state = $State; supervisor_pid = $PID; remote_online_verified = $false }
    [IO.File]::WriteAllText($temporary, ($value | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination (Join-Path $stateDirectory 'status.json') -Force
}

function Invoke-RunnerSupervisor {
    Test-RunnerRegistration | Out-Null
    $hash = [Security.Cryptography.SHA256]::Create()
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    try { $key = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity + $runnerDirectory)))).Replace('-', '') }
    finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\TrafficCDRunner-' + $key))
    $held = $false
    try {
        try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { return }
        $failures = 0
        while (-not (Test-Path -LiteralPath $stopFile)) {
            try { $state = Invoke-RunnerTick; $failures = 0 }
            catch { $state = 'listener_start_failed'; $failures = [Math]::Min(4, $failures + 1) }
            Write-RunnerStatus $state
            Start-Sleep -Seconds ([Math]::Min(120, 15 * [Math]::Pow(2, $failures)))
        }
        Write-RunnerStatus 'supervisor_stopped'
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    switch ($Action) {
        'Check' {
            $value = Get-RunnerStartup
            $status = Join-Path $stateDirectory 'status.json'
            @{ registered_runner_id = (Test-RunnerRegistration); autostart_installed = ($value -eq (Get-RunnerCommand));
               startup = 'current_user_logon'; listener_count = @(Get-RunnerProcesses 'Runner.Listener').Count;
               worker_count = @(Get-RunnerProcesses 'Runner.Worker').Count;
               status = $(if (Test-Path -LiteralPath $status) { Get-Content -LiteralPath $status -Raw | ConvertFrom-Json } else { $null }) } | ConvertTo-Json -Depth 4
        }
        'Install' {
            Test-RunnerRegistration | Out-Null
            $command = Get-RunnerCommand
            $existing = Get-RunnerStartup
            if ($existing -and $existing -ne $command -and $existing -ne (Get-RunnerCommand $PSCommandPath)) { throw 'Startup entry differs; inspect it before replacing.' }
            New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
            $statusFile = Join-Path $stateDirectory 'status.json'
            if (Test-Path -LiteralPath $statusFile) {
                $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
                if ($status.state -ne 'supervisor_stopped' -and (Get-Process -Id $status.supervisor_pid -ErrorAction SilentlyContinue)) {
                    [IO.File]::WriteAllText($stopFile, 'restart supervision only')
                    $deadline = [DateTime]::UtcNow.AddSeconds(135)
                    do {
                        Start-Sleep -Seconds 1
                        $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
                        if ([DateTime]::UtcNow -gt $deadline) { throw 'Previous supervisor has not stopped; the runner remains untouched.' }
                    } while ($status.state -ne 'supervisor_stopped' -and (Get-Process -Id $status.supervisor_pid -ErrorAction SilentlyContinue))
                }
            }
            # Pin an installed copy so switching the development branch cannot
            # remove the startup script. No credential is copied.
            if ($PSCommandPath -ne $installedScript) { Copy-Item -LiteralPath $PSCommandPath -Destination $installedScript -Force }
            [IO.File]::WriteAllText((Join-Path $stateDirectory 'supervisor-root.json'),
                (@{ project_root = $projectRoot } | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
            if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
            New-Item -Path $runKey -Force | Out-Null
            New-ItemProperty -LiteralPath $runKey -Name $entryName -PropertyType String -Value $command -Force | Out-Null
            Start-Process -FilePath 'powershell.exe' -ArgumentList ($command.Substring('powershell.exe '.Length)) -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null
            Write-Output 'Runner supervision installed for the current user logon; existing listener/workers are preserved.'
        }
        'Run' { Invoke-RunnerSupervisor }
        'Uninstall' {
            $existing = Get-RunnerStartup
            if ($existing -and $existing -ne (Get-RunnerCommand)) { throw 'Startup entry differs; it was not removed.' }
            if ($existing) { Remove-ItemProperty -LiteralPath $runKey -Name $entryName }
            New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
            [IO.File]::WriteAllText($stopFile, 'stop supervision only')
            Write-Output 'Supervision will stop. The runner and any active deployment are left running.'
        }
    }
}

# One-time operator setup. Check is read-only; other actions are explicit.
[CmdletBinding()]
param(
    [ValidateSet('Check', 'ConfigureGitHub', 'CreateAccess', 'RegisterRunner', 'RepairRunner', 'StartRunner', 'EnableCD')]
    [string]$Action = 'Check',
    [string]$PullSecretName = '',
    [switch]$PublicImageConfirmed
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$privateDirectory = Join-Path $projectRoot 'results\cicd\private'
$runnerDirectory = Join-Path $projectRoot 'results\cicd\actions-runner'
$runnerWorkDirectory = Join-Path ([IO.Path]::GetPathRoot($projectRoot)) 'traffic-cd-work'
$deliveryConfig = Join-Path $privateDirectory 'kubeconfig.json'
$repository = 'Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21'
$apiRoot = 'https://api.github.com/repos/' + $repository
$script:GitHubHeaders = $null

function Get-ApiHeaders {
    if (-not $script:GitHubHeaders) {
        $credential = "protocol=https`nhost=github.com`n`n" | git -c credential.interactive=never credential fill 2>$null
        $passwordLine = $credential | Where-Object { $_.StartsWith('password=') }
        if (-not $passwordLine) { throw 'No GitHub credential is available in Git Credential Manager.' }
        $script:GitHubHeaders = @{ Accept = 'application/vnd.github+json'; 'X-GitHub-Api-Version' = '2022-11-28'; Authorization = 'Bearer ' + $passwordLine.Substring(9) }
        $credential = $null
        $passwordLine = $null
    }
    return $script:GitHubHeaders
}

function Invoke-GitHub([string]$Method, [string]$Url, $Value = $null, [switch]$AllowMissing) {
    $parameters = @{ Method = $Method; Uri = $Url; Headers = (Get-ApiHeaders); TimeoutSec = 30 }
    if ($null -ne $Value) { $parameters.Body = ($Value | ConvertTo-Json -Depth 15 -Compress); $parameters.ContentType = 'application/json' }
    try { return Invoke-RestMethod @parameters }
    catch {
        $status = [int]$_.Exception.Response.StatusCode
        if ($AllowMissing -and $status -eq 404) { return $null }
        $fields = ''
        try {
            $details = $_.ErrorDetails.Message | ConvertFrom-Json
            $fields = (($details.errors | ForEach-Object { $_.resource + ':' + $_.field + ':' + $_.code }) -join ',')
        } catch { $fields = 'unavailable' }
        throw ('GitHub setup request failed (HTTP ' + $status + ', ' + $Method + ' ' + ([Uri]$Url).AbsolutePath + ', fields=' + $fields + '); raw response omitted.')
    }
}

function Set-VariableValue([string]$Name, [string]$Value) {
    $existing = Invoke-GitHub GET ($apiRoot + '/actions/variables/' + $Name) -AllowMissing
    if ([string]::IsNullOrEmpty($Value)) {
        if ($existing) { Invoke-GitHub DELETE ($apiRoot + '/actions/variables/' + $Name) | Out-Null }
        return
    }
    if ($existing) { Invoke-GitHub PATCH ($apiRoot + '/actions/variables/' + $Name) @{ name = $Name; value = $Value } | Out-Null }
    else { Invoke-GitHub POST ($apiRoot + '/actions/variables') @{ name = $Name; value = $Value } | Out-Null }
}

function Get-Cli {
    $cli = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe'
    if (Test-Path -LiteralPath $cli -PathType Leaf) { return $cli }
    return (Get-Command kubectl -CommandType Application -ErrorAction Stop).Source
}

function Invoke-Kube([string[]]$Arguments) {
    $cli = Get-Cli
    $value = & $cli @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Kubernetes setup/check failed; do not continue.' }
    return ($value -join "`n")
}

function Check-Gate {
    $environment = Invoke-GitHub GET ($apiRoot + '/environments/rke2-production') -AllowMissing
    return [bool]($environment -and ($environment.protection_rules | Where-Object { $_.type -eq 'required_reviewers' -and $_.reviewers.Count -gt 0 }))
}

function New-PrivateDirectory([string]$Directory) {
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Directory -AclObject $acl
}

function Initialize-RunnerWorkDirectory {
    $repoName = ($repository -split '/')[-1]
    $workspace = Join-Path (Join-Path $runnerWorkDirectory $repoName) $repoName
    $packFile = Join-Path $workspace ('.git\objects\pack\pack-' + ('a' * 40) + '.keep')
    if ($packFile.Length -gt 220) { throw 'Runner work directory is still too long; choose a shorter drive-root location.' }
    if (-not (Test-Path -LiteralPath $runnerWorkDirectory)) { New-PrivateDirectory $runnerWorkDirectory }
}

function Configure-LocalRunner([string]$RunnerName) {
    Initialize-RunnerWorkDirectory
    $registration = Invoke-GitHub POST ($apiRoot + '/actions/runners/registration-token')
    Push-Location $runnerDirectory
    try {
        $setupOutput = & ./config.cmd --unattended --url ('https://github.com/' + $repository) --token $registration.token --name $RunnerName --labels 'rke2-deploy' --work $runnerWorkDirectory 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Runner registration failed; raw setup output omitted.' }
    } finally { Pop-Location; $registration = $null; $setupOutput = $null }
}

try {
    if ($Action -eq 'Check') {
        $runners = Invoke-GitHub GET ($apiRoot + '/actions/runners')
        $enabled = Invoke-GitHub GET ($apiRoot + '/actions/variables/RKE2_CD_ENABLED') -AllowMissing
        @{ approval_configured = (Check-Gate); dedicated_kubeconfig = (Test-Path -LiteralPath $deliveryConfig);
           cd_enabled = [bool]($enabled -and $enabled.value -eq 'true');
           registered_runners = @($runners.runners | Select-Object name,status,busy) } | ConvertTo-Json -Depth 5
    }
    elseif ($Action -eq 'ConfigureGitHub') {
        $owner = Invoke-GitHub GET 'https://api.github.com/user'
        $repo = Invoke-GitHub GET $apiRoot
        if (-not $repo.permissions.admin) { throw 'Repository admin permission is required for approval protection.' }
        $body = @{ wait_timer = 0; prevent_self_review = $false; can_admins_bypass = $false;
                   reviewers = @(@{ type = 'User'; id = $owner.id });
                   deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true } }
        Invoke-GitHub PUT ($apiRoot + '/environments/rke2-production') $body | Out-Null
        $policies = Invoke-GitHub GET ($apiRoot + '/environments/rke2-production/deployment-branch-policies')
        if (-not ($policies.branch_policies | Where-Object { $_.name -eq $repo.default_branch })) {
            Invoke-GitHub POST ($apiRoot + '/environments/rke2-production/deployment-branch-policies') @{ name = $repo.default_branch; type = 'branch' } | Out-Null
        }
        $uid = Invoke-Kube -Arguments @('--request-timeout=20s', 'get', 'namespace', 'traffic-mlops', '-o', 'jsonpath={.metadata.uid}')
        Set-VariableValue 'RKE2_CD_ENABLED' 'false'
        Set-VariableValue 'RKE2_CONTEXT' 'traffic-delivery'
        Set-VariableValue 'RKE2_NAMESPACE_UID' $uid.Trim()
        Set-VariableValue 'RKE2_KUBECONFIG' $deliveryConfig
        Set-VariableValue 'RKE2_IMAGE_PULL_SECRET' $PullSecretName
        if (-not (Check-Gate)) { throw 'The required-reviewer gate was not confirmed.' }
        Write-Output 'GitHub approval is configured. Automatic delivery remains disabled.'
    }
    elseif ($Action -eq 'CreateAccess') {
        # Operator invocation explicitly authorizes this one-time RBAC apply.
        Invoke-Kube -Arguments @('apply', '-f', (Join-Path $projectRoot 'deployment\rke2\delivery-rbac.yaml')) | Write-Output
        if (Test-Path -LiteralPath $deliveryConfig) { throw 'Dedicated kubeconfig already exists; review renewal privately instead of overwriting it.' }
        $metadata = Invoke-Kube -Arguments @('config', 'view', '--minify', '--raw', '-o', 'json') | ConvertFrom-Json
        if (@($metadata.clusters).Count -ne 1 -or $metadata.clusters[0].cluster.'insecure-skip-tls-verify') { throw 'The selected cluster/TLS configuration is not supported.' }
        $cluster = $metadata.clusters[0].cluster
        $metadata = $null
        $token = Invoke-Kube -Arguments @('-n', 'traffic-mlops', 'create', 'token', 'traffic-delivery', '--duration=720h')
        New-PrivateDirectory $privateDirectory
        $config = @{ apiVersion = 'v1'; kind = 'Config'; 'current-context' = 'traffic-delivery';
            clusters = @(@{ name = 'rke2'; cluster = $cluster }); users = @(@{ name = 'traffic-delivery'; user = @{ token = $token.Trim() } });
            contexts = @(@{ name = 'traffic-delivery'; context = @{ cluster = 'rke2'; user = 'traffic-delivery'; namespace = 'traffic-mlops' } }) }
        [IO.File]::WriteAllText($deliveryConfig, ($config | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
        $token = $null; $config = $null
        Invoke-Kube -Arguments @('--kubeconfig', $deliveryConfig, 'auth', 'can-i', 'patch', 'deployment/traffic-api', '-n', 'traffic-mlops') | Write-Output
        Write-Output 'Dedicated access created privately. Token lifetime may be capped by the API server; renew before its actual expiry.'
    }
    elseif ($Action -eq 'RegisterRunner') {
        if (Test-Path -LiteralPath (Join-Path $runnerDirectory '.runner')) { throw 'This directory already contains a registered runner; do not replace it.' }
        $downloads = Invoke-GitHub GET ($apiRoot + '/actions/runners/downloads')
        $asset = $downloads | Where-Object { $_.os -eq 'win' -and $_.architecture -eq 'x64' } | Select-Object -First 1
        if (-not $asset.sha256_checksum -or $asset.download_url -notlike 'https://github.com/actions/runner/*') { throw 'No checksum-bound official Windows runner archive is available.' }
        New-PrivateDirectory $runnerDirectory
        $archive = Join-Path $runnerDirectory $asset.filename
        Invoke-WebRequest -Uri $asset.download_url -OutFile $archive -UseBasicParsing -TimeoutSec 180
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256_checksum.ToLowerInvariant()) { throw 'Runner archive checksum differs.' }
        Expand-Archive -LiteralPath $archive -DestinationPath $runnerDirectory -Force
        Configure-LocalRunner ('rke2-' + $env:COMPUTERNAME)
        Write-Output 'Windows runner registered with a short work directory; it has not been started.'
    }
    elseif ($Action -eq 'RepairRunner') {
        $configurationPath = Join-Path $runnerDirectory '.runner'
        $configuration = Get-Content -LiteralPath $configurationPath -Raw | ConvertFrom-Json
        if ($configuration.gitHubUrl.TrimEnd('/') -ne ('https://github.com/' + $repository)) { throw 'The registered runner belongs to another repository.' }
        if ($configuration.workFolder -eq $runnerWorkDirectory) { throw 'The runner already has the short work directory; inspect before reconfiguring.' }
        if ((Test-Path -LiteralPath $runnerWorkDirectory) -and @(Get-ChildItem -LiteralPath $runnerWorkDirectory -Force).Count) {
            throw 'The new work directory is not empty; preserve it and inspect before repair.'
        }
        $runners = Invoke-GitHub GET ($apiRoot + '/actions/runners')
        $registered = $runners.runners | Where-Object { $_.id -eq $configuration.agentId }
        if (-not $registered -or $registered.busy) { throw 'The runner is missing or busy; do not interrupt an active delivery.' }
        $workers = @(Get-Process -Name 'Runner.Worker' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like ($runnerDirectory + '\*') })
        if ($workers.Count) { throw 'A runner worker process is active; do not reconfigure it.' }
        Initialize-RunnerWorkDirectory
        $listeners = @(Get-Process -Name 'Runner.Listener' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like ($runnerDirectory + '\*') })
        foreach ($listener in $listeners) {
            Stop-Process -Id $listener.Id -ErrorAction Stop
            $listener.WaitForExit(10000) | Out-Null
        }
        $removal = Invoke-GitHub POST ($apiRoot + '/actions/runners/remove-token')
        Push-Location $runnerDirectory
        try {
            $setupOutput = & ./config.cmd remove --token $removal.token 2>&1
            if ($LASTEXITCODE -ne 0) { throw 'Runner removal failed; old workspace was not removed.' }
        } finally { Pop-Location; $removal = $null; $setupOutput = $null }
        Configure-LocalRunner $configuration.agentName
        Write-Output 'Runner reconfigured with a short work directory. The old checkout and project workspace were preserved; start the runner again.'
    }
    elseif ($Action -eq 'StartRunner') {
        if (-not (Test-Path -LiteralPath $deliveryConfig)) { throw 'Create dedicated Kubernetes access before starting the runner.' }
        if (-not (Test-Path -LiteralPath (Join-Path $runnerDirectory '.runner'))) { throw 'Register this runner first.' }
        if (Get-Process -Name 'Runner.Listener' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like ($runnerDirectory + '\*') }) { throw 'This runner is already running.' }
        # Node actions must trust the same public roots as Windows on this
        # machine; do not disable TLS verification to work around a local CA.
        $previousCaBundle = $env:NODE_EXTRA_CA_CERTS
        if (-not $previousCaBundle) {
            $pemPath = Join-Path $privateDirectory 'windows-public-roots.pem'
            $certificates = @(Get-ChildItem Cert:\CurrentUser\Root, Cert:\LocalMachine\Root | Sort-Object Thumbprint -Unique)
            $pemText = ($certificates | ForEach-Object { "-----BEGIN CERTIFICATE-----`n" + [Convert]::ToBase64String($_.RawData, [Base64FormattingOptions]::InsertLineBreaks) + "`n-----END CERTIFICATE-----`n" }) -join "`n"
            [IO.File]::WriteAllText($pemPath, $pemText, (New-Object Text.UTF8Encoding($false)))
            $env:NODE_EXTRA_CA_CERTS = $pemPath
        }
        # Child Git processes need long-path support before checkout begins.
        # Scope this to the runner process, preserving the user's Git config.
        $oldGitCount = [Environment]::GetEnvironmentVariable('GIT_CONFIG_COUNT', 'Process')
        $gitCount = 0
        if ($oldGitCount -and -not [int]::TryParse($oldGitCount, [ref]$gitCount)) { throw 'Invalid inherited GIT_CONFIG_COUNT.' }
        $gitKeyName = 'GIT_CONFIG_KEY_' + $gitCount
        $gitValueName = 'GIT_CONFIG_VALUE_' + $gitCount
        $oldGitKey = [Environment]::GetEnvironmentVariable($gitKeyName, 'Process')
        $oldGitValue = [Environment]::GetEnvironmentVariable($gitValueName, 'Process')
        [Environment]::SetEnvironmentVariable('GIT_CONFIG_COUNT', [string]($gitCount + 1), 'Process')
        [Environment]::SetEnvironmentVariable($gitKeyName, 'core.longpaths', 'Process')
        [Environment]::SetEnvironmentVariable($gitValueName, 'true', 'Process')
        try { Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', 'run.cmd') -WorkingDirectory $runnerDirectory -WindowStyle Hidden }
        finally {
            $env:NODE_EXTRA_CA_CERTS = $previousCaBundle
            [Environment]::SetEnvironmentVariable('GIT_CONFIG_COUNT', $oldGitCount, 'Process')
            [Environment]::SetEnvironmentVariable($gitKeyName, $oldGitKey, 'Process')
            [Environment]::SetEnvironmentVariable($gitValueName, $oldGitValue, 'Process')
        }
        Write-Output 'Runner started in the background under the current Windows account.'
    }
    elseif ($Action -eq 'EnableCD') {
        if (-not (Check-Gate)) { throw 'Configure the real production approval gate first.' }
        $runners = Invoke-GitHub GET ($apiRoot + '/actions/runners')
        if (-not ($runners.runners | Where-Object { $_.status -eq 'online' -and ($_.labels.name -contains 'rke2-deploy') })) { throw 'No online rke2-deploy runner is available.' }
        if (-not $PublicImageConfirmed -and -not $PullSecretName) { throw 'Confirm public image pull access or supply the reviewed private registry pull Secret name.' }
        $uid = Invoke-Kube -Arguments @('--kubeconfig', $deliveryConfig, 'get', 'namespace', 'traffic-mlops', '-o', 'jsonpath={.metadata.uid}')
        $expected = Invoke-GitHub GET ($apiRoot + '/actions/variables/RKE2_NAMESPACE_UID')
        if ($uid.Trim() -ne $expected.value) { throw 'Runner cluster identity differs from the GitHub configuration.' }
        Set-VariableValue 'RKE2_IMAGE_PULL_SECRET' $PullSecretName
        Set-VariableValue 'RKE2_CD_ENABLED' 'true'
        Write-Output 'Delivery enabled. Each release still requires one GitHub approval.'
    }
} finally {
    if ($script:GitHubHeaders) { $script:GitHubHeaders.Clear(); $script:GitHubHeaders = $null }
}

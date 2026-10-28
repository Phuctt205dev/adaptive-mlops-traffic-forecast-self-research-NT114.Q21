param(
    [ValidateSet('Setup', 'Login', 'Doctor')]
    [string]$Action = 'Setup',
    [string]$Username = 'truongphucdev'
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$basePython = Join-Path $projectRoot '.venv/Scripts/python.exe'
$probePython = Join-Path $projectRoot 'results/kaggle-poc/venv/Scripts/python.exe'
$probeScript = Join-Path $PSScriptRoot 'kaggle_poc.py'

function Assert-CommandSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed. Stop here and share the error summary, never your token."
    }
}

if ($Action -eq 'Setup') {
    if (-not (Test-Path -LiteralPath $probePython)) {
        if (Test-Path -LiteralPath $basePython) {
            & $basePython -m venv (Join-Path $projectRoot 'results/kaggle-poc/venv')
        } else {
            & py -3 -m venv (Join-Path $projectRoot 'results/kaggle-poc/venv')
        }
        Assert-CommandSuccess 'Create isolated Kaggle environment'
    }
    & $probePython -c "import importlib.metadata as m, sys; v={d.metadata['Name'].lower():d.version for d in m.distributions()}; sys.exit(v.get('kaggle') != '2.2.5' or v.get('kagglesdk') != '0.1.38')"
    if ($LASTEXITCODE -ne 0) {
        & $probePython -m pip install --disable-pip-version-check 'kaggle==2.2.5' 'kagglesdk==0.1.38'
        Assert-CommandSuccess 'Install official Kaggle SDK'
    }
    & $probePython $probeScript doctor --username $Username
    Assert-CommandSuccess 'Check local setup'
    Write-Host ''
    Write-Host 'Setup complete. No notebook has been uploaded or run.'
    Write-Host 'Next: create an API token in Kaggle > Your API tokens, then run:'
    Write-Host ".\scripts\cloud_training\setup-kaggle.ps1 -Action Login -Username $Username"
    exit 0
}

if (-not (Test-Path -LiteralPath $probePython)) {
    throw 'Run setup-kaggle.ps1 -Action Setup first.'
}
if ($Action -eq 'Login') {
    Write-Host 'Paste the API token ONLY into the hidden prompt below, never into chat.'
    & $probePython $probeScript login --username $Username
    Assert-CommandSuccess 'Kaggle login'
}
& $probePython $probeScript doctor --username $Username --online
Assert-CommandSuccess 'Check account and quota'

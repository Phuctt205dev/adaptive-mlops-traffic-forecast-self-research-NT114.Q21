param(
    [Parameter(Mandatory = $true)]
    [string]$ApiBaseUrl,

    [string]$BasePath = "",
    [string]$Branch = "master",
    [string]$WorkflowFile = "deploy-frontend-pages.yml",
    [switch]$AllowHttpApi,
    [switch]$SkipGitHubVariables
)

$ErrorActionPreference = "Stop"

function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found."
    }
}

Assert-Command gh
Assert-Command git

if ($ApiBaseUrl -notmatch "^https?://") {
    throw "ApiBaseUrl must start with http:// or https://."
}

$apiUri = [Uri]$ApiBaseUrl
if ($apiUri.Scheme -eq "http" -and -not $AllowHttpApi) {
    throw @"
GitHub Pages is served over HTTPS, so browsers will block an HTTP API URL as mixed content.
Use an HTTPS API URL from Phase 10, or rerun with -AllowHttpApi only for temporary testing.
"@
}

if (-not $BasePath) {
    $remoteUrl = (git remote get-url origin).Trim()
    $repoName = [IO.Path]::GetFileNameWithoutExtension($remoteUrl)
    if (-not $repoName) {
        throw "Cannot detect repository name from git remote. Pass -BasePath explicitly."
    }
    $BasePath = "/$repoName/"
}

if (-not $BasePath.StartsWith("/")) {
    $BasePath = "/$BasePath"
}
if (-not $BasePath.EndsWith("/")) {
    $BasePath = "$BasePath/"
}

Write-Host "[frontend] API base URL: $ApiBaseUrl"
Write-Host "[frontend] Pages base path: $BasePath"

if (-not $SkipGitHubVariables) {
    gh variable set VITE_API_BASE_URL --body $ApiBaseUrl
    gh variable set VITE_BASE_PATH --body $BasePath
}

gh workflow run $WorkflowFile `
    --ref $Branch `
    -f api_base_url=$ApiBaseUrl `
    -f base_path=$BasePath

Write-Host "[frontend] GitHub Pages deployment workflow triggered."
Write-Host "[frontend] Open GitHub Actions and wait for '$WorkflowFile' to finish."

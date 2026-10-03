param(
    [Parameter(Mandatory = $true)]
    [string]$FrontendUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiBaseUrl,

    [string]$AdminEmail = "admin@example.com",
    [string]$AdminPassword = ""
)

$ErrorActionPreference = "Stop"

function Write-Check {
    param([string]$Message)
    Write-Host "[smoke] $Message"
}

if (-not $FrontendUrl.EndsWith("/")) {
    $FrontendUrl = "$FrontendUrl/"
}
if ($ApiBaseUrl.EndsWith("/")) {
    $ApiBaseUrl = $ApiBaseUrl.TrimEnd("/")
}

Write-Check "frontend page..."
$frontendResponse = Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing
if ($frontendResponse.StatusCode -lt 200 -or $frontendResponse.StatusCode -ge 300) {
    throw "Frontend returned HTTP $($frontendResponse.StatusCode)."
}
if ($frontendResponse.Content -notmatch '<div id="root">') {
    throw "Frontend page does not look like the React app shell."
}
Write-Check "frontend page ok"

Write-Check "api liveness..."
Invoke-RestMethod -Method Get -Uri "$ApiBaseUrl/health/live" | Out-Null
Write-Check "api liveness ok"

Write-Check "api readiness..."
Invoke-RestMethod -Method Get -Uri "$ApiBaseUrl/health/ready" | Out-Null
Write-Check "api readiness ok"

if ($AdminPassword) {
    Write-Check "admin login..."
    $loginBody = @{
        email = $AdminEmail
        password = $AdminPassword
    } | ConvertTo-Json
    $login = Invoke-RestMethod `
        -Method Post `
        -Uri "$ApiBaseUrl/auth/login" `
        -ContentType "application/json" `
        -Body $loginBody

    if (-not $login.access_token) {
        throw "Login response did not contain access_token."
    }
    Write-Check "admin login ok"
}

Write-Host "[smoke] GitHub Pages static site and API endpoint are reachable."

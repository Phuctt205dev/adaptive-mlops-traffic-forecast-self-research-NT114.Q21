param(
    [Parameter(Mandatory = $true)]
    [string]$ApiBaseUrl,

    [string]$AdminEmail = "admin@example.com",

    [Parameter(Mandatory = $true)]
    [string]$AdminPassword,

    [int]$Retries = 30,

    [int]$DelaySeconds = 10
)

$ErrorActionPreference = "Stop"

$ApiBaseUrl = $ApiBaseUrl.TrimEnd("/")

function Invoke-JsonRequest {
    param(
        [string]$Method = "GET",
        [string]$Path,
        [object]$Body = $null,
        [string]$Token = ""
    )

    $headers = @{}
    if ($Token) {
        $headers["Authorization"] = "Bearer $Token"
    }

    $params = @{
        Method = $Method
        Uri = "$ApiBaseUrl$Path"
        Headers = $headers
        TimeoutSec = 30
    }

    if ($null -ne $Body) {
        $params["ContentType"] = "application/json"
        $params["Body"] = ($Body | ConvertTo-Json -Depth 10)
    }

    Invoke-RestMethod @params
}

function Wait-Check {
    param(
        [string]$Name,
        [scriptblock]$Check
    )

    for ($i = 1; $i -le $Retries; $i++) {
        try {
            $result = & $Check
            Write-Host "[smoke] $Name... ok"
            return $result
        } catch {
            if ($i -eq $Retries) {
                throw
            }
            Write-Host "[smoke] $Name... waiting ($i/$Retries)"
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

Wait-Check "liveness" {
    Invoke-JsonRequest -Path "/health/live"
}

$ready = Wait-Check "readiness" {
    $response = Invoke-JsonRequest -Path "/health/ready"
    if ($response.status -ne "ready") {
        throw "API is not ready yet."
    }
    $response
}

$login = Wait-Check "admin login" {
    Invoke-JsonRequest `
        -Method "POST" `
        -Path "/auth/login" `
        -Body @{ email = $AdminEmail; password = $AdminPassword }
}

$summary = Wait-Check "admin system summary" {
    Invoke-JsonRequest `
        -Path "/admin/system/summary" `
        -Token $login.access_token
}

Write-Host ""
Write-Host "API smoke test passed."
Write-Host "Readiness: $($ready.status)"
Write-Host "Totals: $($summary.totals | ConvertTo-Json -Compress)"

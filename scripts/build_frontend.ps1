param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$frontendRoot = Join-Path $ProjectRoot "frontend"
$packageJson = Join-Path $frontendRoot "package.json"
$validationDist = Join-Path $frontendRoot "dist-validation"

if (-not (Test-Path -LiteralPath $packageJson)) {
    throw "Frontend package not found: $packageJson"
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
}
if ($null -eq $npm) {
    throw "npm was not found; frontend production build was not created"
}

Push-Location $frontendRoot
$previousPrivateDashboard = [Environment]::GetEnvironmentVariable("VITE_PRIVATE_DASHBOARD", "Process")
$previousApiBaseUrl = [Environment]::GetEnvironmentVariable("VITE_API_BASE_URL", "Process")
$previousWsUrl = [Environment]::GetEnvironmentVariable("VITE_WS_URL", "Process")
try {
    # This build is the private FastAPI/Tailscale deployment. It must use
    # same-origin relative API polling and must not inherit a public API or
    # WebSocket endpoint from the operator shell.
    $env:VITE_PRIVATE_DASHBOARD = "true"
    $env:VITE_API_BASE_URL = ""
    $env:VITE_WS_URL = ""
    # package.json routes all routine builds to dist-validation, never the
    # frontend/dist directory currently mounted by the production API.
    & $npm.Source run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend production build failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($null -eq $previousPrivateDashboard) {
        Remove-Item Env:VITE_PRIVATE_DASHBOARD -ErrorAction SilentlyContinue
    } else {
        $env:VITE_PRIVATE_DASHBOARD = $previousPrivateDashboard
    }
    if ($null -eq $previousApiBaseUrl) {
        Remove-Item Env:VITE_API_BASE_URL -ErrorAction SilentlyContinue
    } else {
        $env:VITE_API_BASE_URL = $previousApiBaseUrl
    }
    if ($null -eq $previousWsUrl) {
        Remove-Item Env:VITE_WS_URL -ErrorAction SilentlyContinue
    } else {
        $env:VITE_WS_URL = $previousWsUrl
    }
    Pop-Location
}

$index = Join-Path $validationDist "index.html"
if (-not (Test-Path -LiteralPath $index)) {
    throw "Validation build completed without frontend/dist-validation/index.html"
}

Write-Host "Frontend validation build ready at $validationDist; it was not published."

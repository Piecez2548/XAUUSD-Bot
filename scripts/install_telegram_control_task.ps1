param(
    [string]$TaskName = "XAUUSD-AI-Trader-Telegram-Control",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "$((Split-Path -Parent $PSScriptRoot))\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python runtime not found: $PythonPath"
}

$buildScript = Join-Path $ProjectRoot "scripts\build_frontend.ps1"
if (Test-Path -LiteralPath $buildScript) {
    try {
        & $buildScript -ProjectRoot $ProjectRoot
    }
    catch {
        Write-Warning "Frontend production build was not refreshed: $($_.Exception.Message)"
        Write-Warning "Telegram Control installation will continue; the API remains usable without static assets."
    }
}

$taskRun = "`"$PythonPath`" `"$ProjectRoot\main.py`" control"
schtasks.exe /Create /F /SC ONLOGON /TN $TaskName /TR $taskRun /RL LIMITED
Write-Host "Installed $TaskName. It starts only the Telegram Control Service."
Write-Host "Monitoring remains stopped until an authorized /start command is received."
